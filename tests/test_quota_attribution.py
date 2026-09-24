import json
import os
import tempfile
import unittest
from unittest.mock import patch

from konvu_telemetry.quota_attribution import (
    apply_out_of_plan_accounting,
    apply_quota_attribution,
    apply_usage_modes,
)
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
    def test_fractional_reset_jitter_stays_in_the_same_window(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100), session("b", 100)])
            first["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T05:00:00.100000+00:00"
            )
            apply_quota_attribution(first)
            second = snapshot(24, [session("a", 160), session("b", 120)])
            second["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T05:00:00.900000+00:00"
            )
            apply_quota_attribution(second)
            self.assertEqual(
                [
                    row["quota_attribution"]["windows"][0]["estimated_percent"]
                    for row in second["sessions"]
                ],
                [3.0, 1.0],
            )

    def test_codex_spending_cap_does_not_mask_available_subscription_usage(
        self,
    ) -> None:
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
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100), session("b", 100)])
            apply_quota_attribution(first)
            self.assertEqual(
                first["sessions"][0]["quota_attribution"]["state"], "observing"
            )

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
                third["sessions"][0]["quota_attribution"]["windows"][0][
                    "estimated_percent"
                ],
                5.0,
            )
            ledger = json.loads(quota_attribution_path().read_text())
            allocations = ledger["providers"]["claude"]["windows"]
            stored = next(iter(allocations.values()))["allocations"]
            self.assertEqual(stored, {"a": 5.0, "b": 1.0})

    def test_projects_next_ten_only_after_stable_calibration(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            for used, tokens in ((22, 200), (24, 300)):
                next_snapshot = snapshot(used, [session("a", tokens)])
                next_snapshot["sessions"][0]["projected_next_10_usage_tokens"] = 50
                apply_quota_attribution(next_snapshot)
                window = next_snapshot["sessions"][0]["quota_attribution"]["windows"][0]
                self.assertNotIn("projected_next_10_percent", window)

            calibrated = snapshot(26, [session("a", 400)])
            calibrated["sessions"][0]["projected_next_10_usage_tokens"] = 50
            apply_quota_attribution(calibrated)
            window = calibrated["sessions"][0]["quota_attribution"]["windows"][0]
            self.assertEqual(window["estimated_percent"], 6.0)
            self.assertEqual(window["projected_next_10_percent"], 1.0)

    def test_retains_usage_until_the_rounded_provider_limit_advances(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            apply_quota_attribution(snapshot(20, [session("a", 150)]))
            apply_quota_attribution(snapshot(20, [session("a", 250)]))

            advanced = snapshot(21, [session("a", 300)])
            apply_quota_attribution(advanced)

            window = advanced["sessions"][0]["quota_attribution"]["windows"][0]
            self.assertEqual(window["estimated_percent"], 1.0)
            ledger = json.loads(quota_attribution_path().read_text())
            stored = next(iter(ledger["providers"]["claude"]["windows"].values()))
            self.assertEqual(
                stored["calibration_samples"], [{"percent": 1.0, "weight": 200.0}]
            )
            self.assertEqual(stored["pending"], {})

    def test_stable_calibration_estimates_work_before_the_next_provider_tick(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            for used, tokens in ((21, 300), (22, 500), (23, 700)):
                apply_quota_attribution(snapshot(used, [session("a", tokens)]))

            pending = snapshot(23, [session("a", 800)])
            pending["sessions"][0]["projected_next_10_usage_tokens"] = 100
            apply_quota_attribution(pending)

            window = pending["sessions"][0]["quota_attribution"]["windows"][0]
            self.assertEqual(window["estimated_percent"], 3.5)
            self.assertEqual(window["projected_next_10_percent"], 0.5)
            ledger = json.loads(quota_attribution_path().read_text())
            stored = next(iter(ledger["providers"]["claude"]["windows"].values()))
            self.assertEqual(stored["allocations"], {"a": 3.0})
            self.assertEqual(stored["pending"], {"a": 100.0})

    def test_reset_removes_share_and_forecast_from_the_previous_window(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            for used, tokens in ((21, 300), (22, 500), (23, 700)):
                apply_quota_attribution(snapshot(used, [session("a", tokens)]))

            reset = snapshot(0, [session("a", 800)])
            reset["sessions"][0]["projected_next_10_usage_tokens"] = 100
            apply_quota_attribution(reset)

            self.assertEqual(
                reset["sessions"][0]["quota_attribution"],
                {"state": "observing", "windows": []},
            )
            ledger = json.loads(quota_attribution_path().read_text())
            stored = next(iter(ledger["providers"]["claude"]["windows"].values()))
            self.assertEqual(stored["allocations"], {})
            self.assertEqual(stored["calibration_samples"], [])
            self.assertEqual(stored["pending"], {})

    def test_unstable_calibration_does_not_produce_a_forecast(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            for used, tokens in ((22, 110), (24, 210), (26, 310)):
                next_snapshot = snapshot(used, [session("a", tokens)])
                next_snapshot["sessions"][0]["projected_next_10_usage_tokens"] = 50
                apply_quota_attribution(next_snapshot)
            window = next_snapshot["sessions"][0]["quota_attribution"]["windows"][0]
            self.assertNotIn("projected_next_10_percent", window)

    def test_each_window_gets_the_same_interval_and_a_reset_clears_its_ledger(
        self,
    ) -> None:
        def snapshot_with_windows(
            five_hour: float, weekly: float, rows: list[dict[str, object]]
        ) -> dict[str, object]:
            data = snapshot(five_hour, rows)
            data["account_quotas"]["claude"]["windows"].append(
                {
                    "limit_id": "default",
                    "period": "weekly",
                    "used_percent": weekly,
                    "resets_at": "2026-01-07T00:00:00+00:00",
                }
            )
            return data

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(
                snapshot_with_windows(20, 30, [session("a", 100), session("b", 100)])
            )
            second = snapshot_with_windows(
                24, 34, [session("a", 160), session("b", 120)]
            )
            apply_quota_attribution(second)
            self.assertEqual(
                second["sessions"][0]["quota_attribution"]["windows"],
                [
                    {"period": "five_hour", "estimated_percent": 3.0},
                    {"period": "weekly", "estimated_percent": 3.0},
                ],
            )
            apply_quota_attribution(snapshot_with_windows(1, 36, [session("a", 200)]))
            ledger = json.loads(quota_attribution_path().read_text())
            windows = ledger["providers"]["claude"]["windows"]
            five_hour_ledger = next(
                value for key, value in windows.items() if ":five_hour:" in key
            )
            self.assertEqual(five_hour_ledger["allocations"], {})
            self.assertEqual(five_hour_ledger["calibration_samples"], [])

    def test_windows_retain_usage_until_each_one_advances(self) -> None:
        def snapshot_with_windows(
            five_hour: float, weekly: float, tokens: int
        ) -> dict[str, object]:
            data = snapshot(five_hour, [session("a", tokens)])
            data["account_quotas"]["claude"]["windows"].append(
                {
                    "limit_id": "default",
                    "period": "weekly",
                    "used_percent": weekly,
                    "resets_at": "2026-01-07T00:00:00+00:00",
                }
            )
            return data

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot_with_windows(20, 30, 100))
            apply_quota_attribution(snapshot_with_windows(21, 30, 300))
            apply_quota_attribution(snapshot_with_windows(21, 31, 500))

            ledger = json.loads(quota_attribution_path().read_text())
            windows = ledger["providers"]["claude"]["windows"]
            five_hour = next(
                value for key, value in windows.items() if ":five_hour:" in key
            )
            weekly = next(value for key, value in windows.items() if ":weekly:" in key)
            self.assertEqual(five_hour["pending"], {"a": 200.0})
            self.assertEqual(
                weekly["calibration_samples"], [{"percent": 1.0, "weight": 400.0}]
            )
            self.assertEqual(weekly["pending"], {})

    def test_exhausted_plan_spend_starts_when_the_cutoff_is_observed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = {
                "sessions": [
                    {
                        "id": "a",
                        "provider": "claude",
                        "usage_mode": "exhausted",
                        "total_cost_usd": 12.0,
                    }
                ]
            }
            apply_out_of_plan_accounting(first)
            self.assertEqual(first["sessions"][0]["out_of_plan_spend_usd"], 0.0)
            second = {
                "sessions": [
                    {
                        "id": "a",
                        "provider": "claude",
                        "usage_mode": "exhausted",
                        "total_cost_usd": 17.5,
                    }
                ]
            }
            apply_out_of_plan_accounting(second)
            self.assertEqual(second["sessions"][0]["out_of_plan_spend_usd"], 5.5)


if __name__ == "__main__":
    unittest.main()
