"""Tests for quota timer display helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from konvu_telemetry.display import (
    ANSI_ESCAPE,
    compact_status_meter,
    hook_quota_meters,
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
