import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from konvu_telemetry.quota_attribution import apply_quota_attribution, apply_usage_modes
from konvu_telemetry.storage import quota_attribution_path


def session(session_id: str, tokens: int) -> dict[str, object]:
    return {
        "id": session_id,
        "provider": "claude",
        "token_usage": {"input": tokens},
    }


def snapshot(used: float, sessions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "sessions": sessions,
        "account_quotas": {
            "claude": {
                "windows": [
                    {
                        "limit_id": "default",
                        "period": "five_hour",
                        "used_percent": used,
                        "resets_at": "2026-01-01T05:00:00+00:00",
                    }
                ]
            }
        },
    }


class QuotaAttributionTests(unittest.TestCase):
    def test_codex_spending_cap_does_not_mask_available_subscription_usage(self) -> None:
        snapshot_data = {
            "sessions": [{"id": "codex", "provider": "codex"}],
            "account_quotas": {
                "codex": {
                    "ordinary_usage_allowed": True,
                    "spend_control_reached": True,
                    "windows": [
                        {"period": "weekly", "used_percent": 38},
                        {"period": "monthly", "used_percent": 100},
                    ],
                }
            },
        }
        apply_usage_modes(snapshot_data)
        self.assertEqual(snapshot_data["sessions"][0]["usage_mode"], "included")

    def test_starts_observing_then_keeps_finished_session_allocations(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KONVU_LIVE_USAGE_HOME": directory}
        ):
            first = snapshot(20, [session("a", 100), session("b", 100)])
            apply_quota_attribution(first)
            self.assertEqual(first["sessions"][0]["quota_attribution"]["state"], "observing")

            second = snapshot(24, [session("a", 160), session("b", 120)])
            apply_quota_attribution(second)
            shares = {
                row["id"]: row["quota_attribution"]["windows"][0]["estimated_percent"]
                for row in second["sessions"]
            }
            self.assertEqual(shares, {"a": 3.0, "b": 1.0})

            third = snapshot(26, [session("a", 200)])
            apply_quota_attribution(third)
            self.assertEqual(
                third["sessions"][0]["quota_attribution"]["windows"][0]["estimated_percent"],
                5.0,
            )
            ledger = json.loads(quota_attribution_path().read_text())
            allocations = ledger["providers"]["claude"]["windows"]
            stored = next(iter(allocations.values()))["allocations"]
            self.assertEqual(stored, {"a": 5.0, "b": 1.0})


if __name__ == "__main__":
    unittest.main()
