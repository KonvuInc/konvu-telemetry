"""Tests for quota timer display helpers."""

from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

from konvu_telemetry.display import (
    ANSI_ESCAPE,
    claude_statusline_rows,
    compact_advice,
    context_meter,
    compact_status_meter,
    hook_quota_meters,
    relevance_context_meter,
    usage_box_lines,
)


class DisplayTests(unittest.TestCase):
    """Keep CLI and hook quota timers readable and tied to their limit."""

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
            "5h · reset: 1.0h [█░░░░░░] 8.0%  Week · reset: 1.0d [██░░░░░] 35.0%",
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

        self.assertEqual(rows[0], "● Paying · resets in 1.0h")
        self.assertIn("Context", rows[1])
        self.assertIn("52%", rows[1])
        self.assertNotIn("5h", "\n".join(rows))
        self.assertNotIn("Week", "\n".join(rows))

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

        self.assertEqual(rows[1], "│ 🔴 Paying · resets in 1.0d")
        self.assertIn("Credits [██░░░░░] 23.0%", rows[2])
        self.assertIn("Context [█████░░] 68.1%", rows[2])
        self.assertNotIn("Week", "\n".join(rows))
        self.assertNotIn("reset:", "\n".join(rows))


class ContextRelevanceDisplayTests(unittest.TestCase):
    """The Claude CLI context meter and /compact hint follow the AI ratings."""

    analysis = {
        "coverage_percent": 90,
        "relevant_percent": 50.0,
        "drifting_percent": 0.0,
        "stale_percent": 50.0,
        "droppable_percent": 50.0,
        "compact_command": "/compact Keep the PR 91 review.",
    }

    def test_used_part_of_the_bar_splits_by_relevance(self) -> None:
        with patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": ""}):
            meter = relevance_context_meter("Context", 50, 16, self.analysis)

        self.assertIn("\x1b[1;38;5;78m━━━━\x1b[0m", meter)
        self.assertIn("\x1b[1;38;5;203m━━━━\x1b[0m", meter)
        self.assertEqual(ANSI_ESCAPE.sub("", meter), "Context ━━━━━━━━──────── 50%")

    def test_compact_link_sits_on_the_context_line_and_opens_the_panel(self) -> None:
        session = {
            "id": "abc",
            "provider": "claude",
            "context_map": {"analysis": self.analysis},
        }
        with patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": ""}):
            line = context_meter(session, 50)

        self.assertIn("?session=claude%3Aabc&tab=context", line)
        plain = re.sub(r"\x1b\]8;;[^\x1b]*\x1b\\", "", ANSI_ESCAPE.sub("", line))
        self.assertTrue(plain.endswith("·  /compact suggested"))
        self.assertNotIn("drifting or stale", line)

    def test_no_compact_hint_when_little_of_the_context_was_reviewed(self) -> None:
        thin = {**self.analysis, "coverage_percent": 40}

        self.assertIsNone(
            compact_advice({"id": "abc", "context_map": {"analysis": thin}}, 90)
        )

    def test_compact_hint_needs_twenty_window_points_of_waste(self) -> None:
        session = {"id": "abc", "context_map": {"analysis": self.analysis}}

        # Half of 39% used is 19.5 points of the window; half of 40% is 20.
        self.assertIsNone(compact_advice(session, 39))
        self.assertIsNotNone(compact_advice(session, 40))

    def test_context_is_always_the_second_line(self) -> None:
        session = {
            "id": "abc",
            "provider": "claude",
            "usage_mode": "included",
            "context_map": {"analysis": self.analysis},
        }
        with (
            patch.dict(
                os.environ, {"TERM": "xterm-256color", "NO_COLOR": "", "COLUMNS": "200"}
            ),
            patch("konvu_telemetry.display.stored_provider_quotas", return_value={}),
            patch("konvu_telemetry.display.quota_reset_times", return_value={}),
            patch(
                "konvu_telemetry.display.cli_dashboard_line", return_value="dashboard"
            ),
        ):
            rows = claude_statusline_rows(session, 50)

        self.assertTrue(ANSI_ESCAPE.sub("", rows[1]).startswith("Context "))
