"""Tests for quota timer display helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from konvu_telemetry.display import (
    ANSI_ESCAPE,
    claude_statusline_rows,
    compact_duration,
    compact_status_meter,
    hook_quota_meters,
    usage_box_lines,
)


class DisplayTests(unittest.TestCase):
    """Keep CLI and hook quota timers readable and tied to their limit."""

    def test_compact_duration_steps_down_before_rounding_to_zero(self) -> None:
        self.assertEqual(compact_duration(3.2 * 86_400), "3.2d")
        self.assertEqual(compact_duration(0.1 * 86_400), "2.4h")
        self.assertEqual(compact_duration(2.5 * 3_600), "2.5h")
        self.assertEqual(compact_duration(0.1 * 3_600), "6m")
        self.assertEqual(compact_duration(2.2 * 60), "2.2m")
        self.assertEqual(compact_duration(0.1 * 60), "6s")

    def test_cli_meter_centers_the_reset_label_inside_its_fill(self) -> None:
        with patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm-256color"}):
            rendered = compact_status_meter("5h", 8.0, 14, "4.2h")

        self.assertIn("reset: 4.2h", ANSI_ESCAPE.sub("", rendered))
        self.assertIn("\x1b[48;5;", rendered)

    def test_hook_meters_put_each_reset_next_to_its_limit_name(self) -> None:
        quotas = {
            "claude": {
                "windows": [
                    {"period": "five_hour", "resets_at": "2030-01-01T01:00:00+00:00"},
                    {"period": "weekly", "resets_at": "2030-01-02T00:00:00+00:00"},
                ]
            }
        }
        with (
            patch("konvu_telemetry.display.time.time", return_value=1_893_456_000),
            patch(
                "konvu_telemetry.display.stored_provider_quotas", return_value=quotas
            ),
        ):
            rendered = hook_quota_meters(
                "8.0% 5-hour limit; 35.0% weekly limit", "claude"
            )

        self.assertEqual(
            rendered,
            "5h · reset: 1h [█░░░░░░] 8.0%  Week · reset: 1d [██░░░░░] 35.0%",
        )

    def test_paid_claude_hud_shows_reset_and_context_without_limit_meters(self) -> None:
        quotas = {
            "claude": {
                "windows": [
                    {
                        "period": "five_hour",
                        "used_percent": 100.0,
                        "resets_at": "2030-01-01T01:00:00+00:00",
                    },
                    {
                        "period": "weekly",
                        "used_percent": 35.0,
                        "resets_at": "2030-01-02T00:00:00+00:00",
                    },
                ]
            }
        }
        session = {
            "usage_mode": "exhausted",
            "out_of_plan_spend_usd": 1.3,
            "projected_next_10_tasks_usd": 0.8,
        }
        with (
            patch.dict(os.environ, {"NO_COLOR": "1"}),
            patch("konvu_telemetry.display.time.time", return_value=1_893_456_000),
            patch(
                "konvu_telemetry.display.stored_provider_quotas", return_value=quotas
            ),
            patch("konvu_telemetry.display.dashboard_line", return_value="dashboard"),
        ):
            rows = claude_statusline_rows(session, 52.4)

        self.assertEqual(rows[0], "● Paying · resets in 1h")
        self.assertIn("Context", rows[1])
        self.assertIn("52%", rows[1])
        self.assertNotIn("5h", "\n".join(rows))
        self.assertNotIn("Week", "\n".join(rows))

    def test_every_claude_hud_state_uses_the_thin_context_bar(self) -> None:
        for usage_mode in ("included", "api_billed", "exhausted", None):
            with self.subTest(usage_mode=usage_mode):
                session = {"provider": "claude", "usage_mode": usage_mode}
                with (
                    patch(
                        "konvu_telemetry.display.stored_provider_quotas",
                        return_value={},
                    ),
                    patch(
                        "konvu_telemetry.display.dashboard_line",
                        return_value="dashboard",
                    ),
                ):
                    rows = claude_statusline_rows(session, 7.0)

                plain = [ANSI_ESCAPE.sub("", row) for row in rows]
                self.assertIn("Context ━─────────────── 7%", plain)
                self.assertNotIn("■", "".join(plain))

    def test_paid_codex_hook_keeps_credits_and_moves_reset_to_paying_line(self) -> None:
        quotas = {
            "codex": {
                "windows": [
                    {
                        "period": "weekly",
                        "used_percent": 100.0,
                        "resets_at": "2030-01-02T00:00:00+00:00",
                    },
                    {
                        "period": "monthly",
                        "used_percent": 23.0,
                        "resets_at": "2030-02-01T00:00:00+00:00",
                    },
                ]
            }
        }
        session = {
            "usage_mode": "exhausted",
            "context_tokens": 681,
            "context_window_tokens": 1000,
        }
        with (
            patch("konvu_telemetry.display.time.time", return_value=1_893_456_000),
            patch(
                "konvu_telemetry.display.stored_provider_quotas", return_value=quotas
            ),
            patch("konvu_telemetry.display.dashboard_line", return_value="dashboard"),
        ):
            rows = usage_box_lines(
                session,
                "100.0% weekly limit · 23.0% monthly limit",
                "codex",
            )

        self.assertEqual(rows[1], "│ 🔴 Paying · resets in 1d")
        self.assertIn("Credits [██░░░░░] 23.0%", rows[2])
        self.assertIn("Context [█████░░] 68.1%", rows[2])
        self.assertNotIn("Week", "\n".join(rows))
        self.assertNotIn("reset:", "\n".join(rows))
