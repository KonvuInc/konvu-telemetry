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


def session(session_id: str, weight: float) -> dict[str, object]:
    """Weight is recorded cost now, not a token count."""
    return {
        "id": session_id,
        "provider": "claude",
        "total_cost_usd": float(weight),
        "cost_status": "complete",
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


def ledger_window(
    ledger: dict[str, object], provider: str, period: str
) -> dict[str, object]:
    providers = ledger["providers"]
    assert isinstance(providers, dict)
    provider_state = providers[provider]
    assert isinstance(provider_state, dict)
    windows = provider_state["windows"]
    assert isinstance(windows, dict)
    return next(
        value
        for key, value in windows.items()
        if isinstance(key, str)
        and isinstance(value, dict)
        and json.loads(key)[1] == period
    )


class QuotaAttributionTests(unittest.TestCase):
    def test_session_reactivation_keeps_its_previous_usage_baseline(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100)])
            first["generated_at"] = "2026-01-01T00:00:00+00:00"
            apply_quota_attribution(first)

            absent = snapshot(20, [])
            absent["generated_at"] = "2026-01-01T00:01:00+00:00"
            apply_quota_attribution(absent)

            returned = snapshot(21, [session("a", 200)])
            returned["generated_at"] = "2026-01-01T00:02:00+00:00"
            apply_quota_attribution(returned)

            attribution = returned["sessions"][0]["quota_attribution"]
            self.assertEqual(attribution["state"], "estimated")
            self.assertEqual(attribution["windows"][0]["estimated_percent"], 1.0)

    def test_reset_jitter_across_a_minute_boundary_keeps_the_window(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100), session("b", 100)])
            first["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T05:00:00.342591+00:00"
            )
            apply_quota_attribution(first)
            second = snapshot(24, [session("a", 160), session("b", 120)])
            second["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T04:59:59.942803+00:00"
            )
            apply_quota_attribution(second)
            self.assertEqual(
                [
                    row["quota_attribution"]["windows"][0]["estimated_percent"]
                    for row in second["sessions"]
                ],
                [3.0, 1.0],
            )

            ledger = json.loads(quota_attribution_path().read_text())
            self.assertEqual(
                len(ledger["providers"]["claude"]["windows"]),
                1,
            )

    def test_bounded_reset_corrections_do_not_accumulate_into_a_false_reset(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100)])
            first["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T05:00:00+00:00"
            )
            apply_quota_attribution(first)
            for used, tokens, reset in (
                (22, 150, "2026-01-01T05:00:50+00:00"),
                (24, 200, "2026-01-01T05:01:40+00:00"),
            ):
                current = snapshot(used, [session("a", tokens)])
                current["account_quotas"]["claude"]["windows"][0]["resets_at"] = reset
                apply_quota_attribution(current)

            self.assertEqual(
                current["sessions"][0]["quota_attribution"]["windows"][0][
                    "estimated_percent"
                ],
                4.0,
            )

    def test_true_reset_uses_the_advanced_deadline_not_a_small_usage_drop(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            increased = snapshot(24, [session("a", 200)])
            apply_quota_attribution(increased)

            correction = snapshot(23.9, [session("a", 250)])
            apply_quota_attribution(correction)
            self.assertEqual(
                correction["sessions"][0]["quota_attribution"]["windows"][0][
                    "estimated_percent"
                ],
                6.0,
            )

            reset = snapshot(1, [session("a", 300)])
            reset["generated_at"] = "2026-01-01T05:01:00+00:00"
            reset["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T10:00:00+00:00"
            )
            apply_quota_attribution(reset)
            self.assertEqual(
                reset["sessions"][0]["quota_attribution"],
                {
                    "state": "observing",
                    "windows": [],
                    "reason": "window_reset",
                },
            )

    def test_future_deadline_correction_does_not_reset_before_the_old_deadline(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100)])
            first["generated_at"] = "2026-01-01T00:00:00+00:00"
            apply_quota_attribution(first)
            increased = snapshot(24, [session("a", 200)])
            increased["generated_at"] = "2026-01-01T00:02:00+00:00"
            apply_quota_attribution(increased)

            correction = snapshot(25, [session("a", 250)])
            correction["generated_at"] = "2026-01-01T00:04:00+00:00"
            correction["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T10:00:00+00:00"
            )
            apply_quota_attribution(correction)

            self.assertEqual(
                correction["sessions"][0]["quota_attribution"]["windows"][0][
                    "estimated_percent"
                ],
                5.0,
            )

    def test_usage_drop_resets_when_the_current_deadline_is_unavailable(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            apply_quota_attribution(snapshot(24, [session("a", 200)]))
            reset = snapshot(1, [session("a", 250)])
            reset["account_quotas"]["claude"]["windows"][0]["resets_at"] = None

            apply_quota_attribution(reset)

            self.assertEqual(
                reset["sessions"][0]["quota_attribution"],
                {
                    "state": "observing",
                    "windows": [],
                    "reason": "window_reset",
                },
            )

            resumed = snapshot(2, [session("a", 300)])
            resumed["generated_at"] = "2026-01-01T05:02:00+00:00"
            resumed["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T10:00:00+00:00"
            )
            apply_quota_attribution(resumed)
            self.assertEqual(
                resumed["sessions"][0]["quota_attribution"]["windows"][0][
                    "estimated_percent"
                ],
                1.0,
            )

    def test_migrates_the_richest_legacy_window_without_losing_estimates(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            quota_attribution_path().parent.mkdir(parents=True, exist_ok=True)
            quota_attribution_path().write_text(
                json.dumps(
                    {
                        "version": 3,
                        "providers": {
                            "claude": {
                                "sessions": {"a": {"cost": 200, "credits": None}},
                                "windows": {
                                    "default:five_hour:2026-01-01T04:59:00+00:00": {
                                        "used_percent": 24,
                                        "allocations": {"a": 4},
                                        "calibration_samples": [
                                            {"percent": 4, "weight": 100}
                                        ],
                                        "pending": {},
                                    },
                                    "default:five_hour:2026-01-01T05:00:00+00:00": {
                                        "used_percent": 24,
                                        "allocations": {},
                                        "pending": {},
                                    },
                                },
                            }
                        },
                    }
                )
            )
            current = snapshot(24, [session("a", 200)])
            current["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T05:00:00.342591+00:00"
            )

            apply_quota_attribution(current)

            self.assertEqual(
                current["sessions"][0]["quota_attribution"]["windows"][0][
                    "estimated_percent"
                ],
                4.0,
            )
            ledger = json.loads(quota_attribution_path().read_text())
            self.assertEqual(ledger["version"], 4)
            self.assertEqual(
                len(ledger["providers"]["claude"]["windows"]),
                1,
            )

    def test_does_not_migrate_legacy_evidence_from_an_expired_window(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            quota_attribution_path().write_text(
                json.dumps(
                    {
                        "version": 3,
                        "providers": {
                            "claude": {
                                "sessions": {"a": {"cost": 200, "credits": None}},
                                "windows": {
                                    "default:five_hour:2026-01-01T05:00:00+00:00": {
                                        "used_percent": 24,
                                        "allocations": {"a": 4},
                                        "pending": {},
                                    }
                                },
                            }
                        },
                    }
                )
            )
            current = snapshot(1, [session("a", 200)])
            current["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T10:00:00+00:00"
            )

            apply_quota_attribution(current)

            self.assertEqual(
                current["sessions"][0]["quota_attribution"],
                {
                    "state": "observing",
                    "windows": [],
                    "reason": "establishing_baseline",
                },
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
            self.assertEqual(
                first["sessions"][0]["quota_attribution"]["reason"],
                "establishing_baseline",
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

    def test_projects_next_ten_after_first_real_calibration(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            for used, tokens in ((22, 200), (24, 300)):
                next_snapshot = snapshot(used, [session("a", tokens)])
                next_snapshot["sessions"][0]["projected_next_10_tasks_usd"] = 50
                apply_quota_attribution(next_snapshot)
                window = next_snapshot["sessions"][0]["quota_attribution"]["windows"][0]
                self.assertEqual(window["projected_next_10_percent"], 1.0)

            calibrated = snapshot(26, [session("a", 400)])
            calibrated["sessions"][0]["projected_next_10_tasks_usd"] = 50
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
            pending["sessions"][0]["projected_next_10_tasks_usd"] = 100
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
            reset["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T10:00:00+00:00"
            )
            reset["sessions"][0]["projected_next_10_tasks_usd"] = 100
            apply_quota_attribution(reset)

            self.assertEqual(
                reset["sessions"][0]["quota_attribution"],
                {
                    "state": "observing",
                    "windows": [],
                    "reason": "window_reset",
                },
            )
            ledger = json.loads(quota_attribution_path().read_text())
            stored = next(iter(ledger["providers"]["claude"]["windows"].values()))
            self.assertEqual(stored["allocations"], {})
            self.assertEqual(stored["calibration_samples"], [])
            self.assertEqual(stored["pending"], {})

    def test_rounded_provider_ticks_use_the_aggregate_calibration_rate(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            for used, tokens in ((22, 110), (24, 210), (26, 310)):
                next_snapshot = snapshot(used, [session("a", tokens)])
                next_snapshot["sessions"][0]["projected_next_10_tasks_usd"] = 50
                apply_quota_attribution(next_snapshot)
            window = next_snapshot["sessions"][0]["quota_attribution"]["windows"][0]
            self.assertEqual(window["projected_next_10_percent"], 1.43)

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
                    {
                        "period": "five_hour",
                        "estimated_percent": 3.0,
                        "scope": "observed_window",
                    },
                    {
                        "period": "weekly",
                        "estimated_percent": 3.0,
                        "scope": "observed_window",
                    },
                ],
            )
            reset = snapshot_with_windows(1, 36, [session("a", 200)])
            reset["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T10:00:00+00:00"
            )
            apply_quota_attribution(reset)
            ledger = json.loads(quota_attribution_path().read_text())
            five_hour_ledger = ledger_window(ledger, "claude", "five_hour")
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
            five_hour = ledger_window(ledger, "claude", "five_hour")
            weekly = ledger_window(ledger, "claude", "weekly")
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


class QuotaWeightTests(unittest.TestCase):
    def test_forecast_never_exceeds_the_window_headroom(self) -> None:
        """Whatever the calibrated rate says, a share of a window cannot exceed
        what is left of that window."""
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(90, [session("a", 100)]))
            advanced = snapshot(97, [session("a", 200)])
            advanced["sessions"][0]["projected_next_10_tasks_usd"] = 1_000_000.0
            apply_quota_attribution(advanced)
            window = advanced["sessions"][0]["quota_attribution"]["windows"][0]
            self.assertLessEqual(window["projected_next_10_percent"], 3.0)

    def test_cost_weighting_ignores_cache_read_volume(self) -> None:
        """Two sessions with identical cost weigh the same, however many tokens
        each re-read from cache."""
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100), session("b", 100)])
            first["sessions"][0]["token_usage"] = {"cache_read": 500_000_000}
            first["sessions"][1]["token_usage"] = {"cache_read": 1_000}
            apply_quota_attribution(first)
            second = snapshot(24, [session("a", 200), session("b", 200)])
            apply_quota_attribution(second)
            shares = [
                row["quota_attribution"]["windows"][0]["estimated_percent"]
                for row in second["sessions"]
            ]
            self.assertEqual(shares[0], shares[1])
