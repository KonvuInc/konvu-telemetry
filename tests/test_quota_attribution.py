import json
import os
import tempfile
import unittest
from unittest.mock import patch

from konvu_telemetry.quota_attribution import (
    ANALYSIS_USAGE_RETENTION_SECONDS,
    apply_out_of_plan_accounting,
    apply_quota_attribution,
    apply_usage_modes,
    record_analysis_usage,
)
from konvu_telemetry.storage import analysis_usage_path, quota_attribution_path

RUN_STARTED_AT = 1_767_225_610.0
RUN_COMPLETED_AT = 1_767_225_620.0


def session(
    session_id: str, weight: float, prompts: float | None = None
) -> dict[str, object]:
    """Weight is recorded cost; prompts drive the ten-prompt projection."""
    row: dict[str, object] = {
        "id": session_id,
        "provider": "claude",
        "total_cost_usd": float(weight),
        "cost_status": "complete",
    }
    if prompts is not None:
        row["task_count"] = prompts
    return row


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
    def test_analysis_usage_ledger_prunes_events_outside_retention(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            reference = RUN_COMPLETED_AT + ANALYSIS_USAGE_RETENTION_SECONDS + 60
            record_analysis_usage(
                "claude", "a", "old", RUN_STARTED_AT, RUN_COMPLETED_AT, 100
            )
            record_analysis_usage(
                "claude", "a", "recent", reference - 20, reference - 10, 100
            )

            usage = json.loads(analysis_usage_path().read_text())

            self.assertEqual(set(usage["events"]), {"recent"})

    def test_rerecording_a_run_adds_missing_price_without_duplicating_it(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            record_analysis_usage(
                "claude", "a", "run-1", RUN_STARTED_AT, RUN_COMPLETED_AT, 100
            )
            record_analysis_usage(
                "claude", "a", "run-1", RUN_STARTED_AT, RUN_COMPLETED_AT, 100,
                cost_usd=0.02,
            )
            record_analysis_usage(
                "claude", "a", "run-1", RUN_STARTED_AT, RUN_COMPLETED_AT, 100,
                cost_usd=0.09,
            )

            events = json.loads(analysis_usage_path().read_text())["events"]

            self.assertEqual(list(events), ["run-1"])
            self.assertEqual(events["run-1"]["cost_usd"], 0.02)
            self.assertEqual(events["run-1"]["tokens"], 100)

    def test_analysis_usage_is_a_hidden_child_of_its_parent_session(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100), session("b", 100)])
            first["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:00:00+00:00"
            )
            apply_quota_attribution(first)
            record_analysis_usage(
                "claude", "a", "run-1", RUN_STARTED_AT, RUN_COMPLETED_AT, 100
            )

            second = snapshot(21, [session("a", 200), session("b", 300)])
            second["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:01:00+00:00"
            )
            apply_quota_attribution(second)

            a_window = second["sessions"][0]["quota_attribution"]["windows"][0]
            b_window = second["sessions"][1]["quota_attribution"]["windows"][0]
            self.assertEqual(a_window["estimated_percent"], 0.5)
            self.assertEqual(a_window["analysis_estimated_percent"], 0.25)
            self.assertEqual(second["sessions"][0]["quota_attribution"]["analysis_run_count"], 1)
            self.assertEqual(b_window["estimated_percent"], 0.5)

    def test_analysis_usage_sums_runs_tokens_cost_and_measured_share(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100), session("b", 100)])
            first["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:00:00+00:00"
            )
            apply_quota_attribution(first)
            breakdown = {
                "input": 10,
                "output": 40,
                "reasoning_output": 0,
                "cache_write": 50,
                "cache_read": 0,
            }
            record_analysis_usage(
                "claude", "a", "in-plan", RUN_STARTED_AT, RUN_COMPLETED_AT, 100,
                model="claude-haiku-4-5", usage_mode="included",
                token_usage=breakdown, cost_usd=0.12,
            )
            record_analysis_usage(
                "claude", "a", "paid", RUN_STARTED_AT, RUN_COMPLETED_AT, 100,
                model="claude-haiku-4-5", usage_mode="exhausted",
                token_usage=breakdown, cost_usd=0.05,
            )

            second = snapshot(21, [session("a", 200), session("b", 300)])
            second["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:01:00+00:00"
            )
            apply_quota_attribution(second)

            usage = second["sessions"][0]["analysis_usage"]
            self.assertEqual(usage["run_count"], 2)
            self.assertEqual(usage["included_run_count"], 1)
            self.assertEqual(usage["spending_run_count"], 1)
            self.assertEqual(usage["tokens"]["total"], 200)
            self.assertEqual(usage["tokens"]["cache_write"], 100)
            self.assertAlmostEqual(usage["cost_usd"], 0.17)
            self.assertAlmostEqual(usage["included_value_usd"], 0.12)
            self.assertAlmostEqual(usage["spending_usd"], 0.05)
            self.assertEqual(
                usage["limit"],
                [{"period": "five_hour", "measured_percent": 0.4, "unreported": None}],
            )
            self.assertNotIn("analysis_usage", second["sessions"][1])

    def test_analysis_usage_below_one_point_is_reported_as_a_bound(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100)])
            first["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:00:00+00:00"
            )
            apply_quota_attribution(first)
            record_analysis_usage(
                "claude", "a", "run-1", RUN_STARTED_AT, RUN_COMPLETED_AT, 100
            )

            second = snapshot(20, [session("a", 200)])
            second["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:01:00+00:00"
            )
            apply_quota_attribution(second)

            usage = second["sessions"][0]["analysis_usage"]
            self.assertEqual(usage["unpriced_run_count"], 1)
            self.assertEqual(usage["unknown_mode_run_count"], 1)
            self.assertEqual(
                usage["limit"],
                [
                    {
                        "period": "five_hour",
                        "measured_percent": 0.0,
                        "unreported": {
                            "below_percent": 1.0,
                            "share_of_unreported_work": 0.5,
                        },
                    }
                ],
            )

    def test_analysis_usage_only_targets_the_primary_subscription_window(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100)])
            first["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:00:00+00:00"
            )
            first["account_quotas"]["claude"]["windows"].append(
                {
                    "limit_id": "default",
                    "period": "weekly",
                    "used_percent": 30,
                    "resets_at": "2026-01-08T00:00:00+00:00",
                }
            )
            apply_quota_attribution(first)
            record_analysis_usage(
                "claude", "a", "run-1", RUN_STARTED_AT, RUN_COMPLETED_AT, 100
            )

            second = snapshot(21, [session("a", 100)])
            second["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:01:00+00:00"
            )
            second["account_quotas"]["claude"]["windows"].append(
                {
                    "limit_id": "default",
                    "period": "weekly",
                    "used_percent": 31,
                    "resets_at": "2026-01-08T00:00:00+00:00",
                }
            )
            apply_quota_attribution(second)

            windows = second["sessions"][0]["quota_attribution"]["windows"]
            five_hour = next(row for row in windows if row["period"] == "five_hour")
            self.assertEqual(five_hour["analysis_estimated_percent"], 1.0)
            ledger = json.loads(quota_attribution_path().read_text())
            weekly = ledger_window(ledger, "claude", "weekly")
            self.assertFalse(
                any(
                    str(session_id).startswith("analysis:")
                    for field in ("pending", "allocations")
                    for session_id in weekly.get(field, {})
                )
            )

    def test_analysis_usage_waits_for_a_provider_observation_after_it_finished(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100)])
            first["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:00:00+00:00"
            )
            apply_quota_attribution(first)
            record_analysis_usage(
                "claude", "a", "run-1", RUN_STARTED_AT, RUN_COMPLETED_AT, 100
            )

            too_early = snapshot(21, [session("a", 200)])
            too_early["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:00:15+00:00"
            )
            apply_quota_attribution(too_early)
            self.assertNotIn(
                "analysis_estimated_percent",
                too_early["sessions"][0]["quota_attribution"]["windows"][0],
            )

            eligible = snapshot(21, [session("a", 200)])
            eligible["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:00:30+00:00"
            )
            apply_quota_attribution(eligible)
            window = eligible["sessions"][0]["quota_attribution"]["windows"][0]
            self.assertTrue(window["analysis_pending"])

    def test_analysis_usage_event_is_added_to_pending_only_once(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100)])
            first["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:00:00+00:00"
            )
            apply_quota_attribution(first)
            record_analysis_usage(
                "claude", "a", "run-1", RUN_STARTED_AT, RUN_COMPLETED_AT, 100
            )

            for second in range(2):
                current = snapshot(20, [session("a", 100)])
                current["account_quotas"]["claude"]["observed_at"] = (
                    "2026-01-01T00:00:30+00:00"
                )
                apply_quota_attribution(current)

            ledger = json.loads(quota_attribution_path().read_text())
            pending = ledger_window(ledger, "claude", "five_hour")["pending"]
            self.assertEqual(pending["analysis:a"], 100)

    def test_window_reset_clears_the_analysis_child_share(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            first = snapshot(20, [session("a", 100)])
            first["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:00:00+00:00"
            )
            apply_quota_attribution(first)
            record_analysis_usage(
                "claude", "a", "run-1", RUN_STARTED_AT, RUN_COMPLETED_AT, 100
            )
            increased = snapshot(21, [session("a", 100)])
            increased["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T00:00:30+00:00"
            )
            apply_quota_attribution(increased)
            self.assertEqual(
                increased["sessions"][0]["quota_attribution"]["windows"][0][
                    "analysis_estimated_percent"
                ],
                1.0,
            )

            reset = snapshot(0, [session("a", 100)])
            reset["generated_at"] = "2026-01-01T05:01:00+00:00"
            reset["account_quotas"]["claude"]["observed_at"] = (
                "2026-01-01T05:01:00+00:00"
            )
            reset["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T10:00:00+00:00"
            )
            apply_quota_attribution(reset)
            self.assertEqual(reset["sessions"][0]["quota_attribution"]["windows"], [])

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
                4.0,
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

    def test_new_deadline_with_a_usage_drop_replaces_the_old_window(self) -> None:
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

            replacement = snapshot(1, [session("a", 300)])
            replacement["generated_at"] = "2026-01-01T00:04:00+00:00"
            replacement["account_quotas"]["claude"]["windows"][0]["resets_at"] = (
                "2026-01-01T10:00:00+00:00"
            )
            apply_quota_attribution(replacement)

            self.assertEqual(
                replacement["sessions"][0]["quota_attribution"],
                {
                    "state": "observing",
                    "windows": [],
                    "reason": "window_reset",
                },
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

    def test_projects_what_the_last_ten_prompts_burned(self) -> None:
        """Twenty prompts that cost this session four points; the last ten of
        them cost two, so the next ten are projected at two."""
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 0, prompts=0)]))
            for step in range(1, 21):
                used = 20 + step // 5
                apply_quota_attribution(
                    snapshot(used, [session("a", step * 10.0, prompts=step)])
                )
            latest = snapshot(24, [session("a", 200.0, prompts=20)])
            apply_quota_attribution(latest)
            window = latest["sessions"][0]["quota_attribution"]["windows"][0]

        self.assertEqual(window["estimated_percent"], 4.0)
        self.assertEqual(window["projected_next_10_percent"], 2.0)

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
            self.assertEqual(stored["allocations"], {"a": 1.0})
            self.assertEqual(stored["pending"], {})

    def test_the_share_holds_still_until_the_provider_reports_again(
        self,
    ) -> None:
        """Work done since the last report has no measured cost, so it is not
        priced into the share. The share is the session's cut of the points the
        provider has actually reported, and it waits for the next one."""
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            for used, tokens in ((21, 300), (22, 500), (23, 700)):
                apply_quota_attribution(snapshot(used, [session("a", tokens)]))

            pending = snapshot(23, [session("a", 800)])
            apply_quota_attribution(pending)

            window = pending["sessions"][0]["quota_attribution"]["windows"][0]
            self.assertEqual(window["estimated_percent"], 3.0)
            ledger = json.loads(quota_attribution_path().read_text())
            stored = next(iter(ledger["providers"]["claude"]["windows"].values()))
            self.assertEqual(stored["allocations"], {"a": 3.0})
            self.assertEqual(stored["pending"], {"a": 100.0})

    def test_a_rise_with_no_recorded_work_is_not_thrown_away(self) -> None:
        """One burst of work can raise the reported percentage twice.

        The first rise empties the tally of work done since the last one, so the
        second arrives with nothing recorded against it. It still happened, so it
        goes to whoever this window already credits rather than to nobody.
        """
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 100)]))
            apply_quota_attribution(snapshot(21, [session("a", 130)]))
            apply_quota_attribution(snapshot(22, [session("b", 40)]))
            # A third rise while neither session's spend has moved at all.
            apply_quota_attribution(snapshot(23, [session("a", 130), session("b", 40)]))

            ledger = json.loads(quota_attribution_path().read_text())
            stored = ledger_window(ledger, "claude", "five_hour")
            allocations = stored["allocations"]

        assert isinstance(allocations, dict)
        self.assertAlmostEqual(sum(allocations.values()), 3.0, places=6)

    def test_a_rise_is_split_by_tokens_rather_than_by_price(self) -> None:
        """Two sessions, equal tokens, but one on a model that costs four times more.

        A token costs about the same slice of the limit whichever model spent it,
        so an equal split is right and a price-weighted one would hand the dearer
        session four fifths of the rise.
        """
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):

            def pair(cheap_cost: float, dear_cost: float, tokens: int) -> list[dict]:
                rows = []
                for name, cost in (("cheap", cheap_cost), ("dear", dear_cost)):
                    row = session(name, cost)
                    row["token_usage"] = {"input": tokens, "output": 0}
                    rows.append(row)
                return rows

            apply_quota_attribution(snapshot(20, pair(0.0, 0.0, 0)))
            apply_quota_attribution(snapshot(21, pair(1.0, 4.0, 1_000_000)))

            ledger = json.loads(quota_attribution_path().read_text())
            allocations = ledger_window(ledger, "claude", "five_hour")["allocations"]

        assert isinstance(allocations, dict)
        self.assertAlmostEqual(allocations["cheap"], 0.5, places=6)
        self.assertAlmostEqual(allocations["dear"], 0.5, places=6)

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
            self.assertEqual(stored["history"], {})
            self.assertEqual(stored["pending"], {})

    def test_a_session_with_under_ten_prompts_is_scaled_to_ten(self) -> None:
        """Five prompts costing one point project two over the next ten."""
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(20, [session("a", 0, prompts=0)]))
            for step in range(1, 6):
                apply_quota_attribution(
                    snapshot(
                        20 + (step == 5), [session("a", step * 10.0, prompts=step)]
                    )
                )
            latest = snapshot(21, [session("a", 50.0, prompts=5)])
            apply_quota_attribution(latest)
            window = latest["sessions"][0]["quota_attribution"]["windows"][0]

        self.assertEqual(window["projected_next_10_percent"], 2.0)

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
            self.assertEqual(five_hour_ledger["history"], {})

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
            self.assertEqual(weekly["allocations"], {"a": 1.0})
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

    def test_subagent_spend_starts_at_the_observed_plan_exit(self) -> None:
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
                        "subagents": [
                            {"id": "old", "cost_usd": 10.0},
                        ],
                    }
                ]
            }
            apply_out_of_plan_accounting(first)
            self.assertEqual(first["sessions"][0]["out_of_plan_subagent_cost_usd"], 0.0)

            second = {
                "sessions": [
                    {
                        "id": "a",
                        "provider": "claude",
                        "usage_mode": "exhausted",
                        "total_cost_usd": 17.5,
                        "subagents": [
                            {"id": "old", "cost_usd": 12.0},
                            {"id": "new", "cost_usd": 3.5},
                        ],
                    }
                ]
            }
            apply_out_of_plan_accounting(second)

        row = second["sessions"][0]
        self.assertEqual(row["out_of_plan_subagent_cost_usd"], 5.5)
        agents = row["subagents"]
        assert isinstance(agents, list)
        self.assertEqual(agents[0]["out_of_plan_cost_usd"], 2.0)
        self.assertEqual(agents[1]["out_of_plan_cost_usd"], 3.5)


if __name__ == "__main__":
    unittest.main()


class QuotaWeightTests(unittest.TestCase):
    def test_forecast_never_exceeds_the_window_headroom(self) -> None:
        """However fast the last ten prompts were going, a share of a window
        cannot exceed what is left of that window."""
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
        ):
            apply_quota_attribution(snapshot(90, [session("a", 100, prompts=1)]))
            advanced = snapshot(97, [session("a", 200, prompts=2)])
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
