import json
import os
import sys
import tempfile
from threading import Lock
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from konvu_telemetry.analytics import (
    apply_notification_tracking,
    baseline_comparison,
    build_baselines,
    cumulative_median_checkpoints,
    deduplicate_usage_events,
    scaled_precompact_forecast,
    single_configuration,
    task_series,
)
from konvu_telemetry.config import BASELINE_MILESTONES
from konvu_telemetry.display import baseline_text
from konvu_telemetry.fleet_telemetry import _timestamp, is_claude_prompt
from konvu_telemetry.live import CodexLiveFile, IncrementalLiveState
from konvu_telemetry.models import Usage, UsageEvent
from konvu_telemetry.parsers import (
    assistant_event,
    codex_events_in_file,
    events_in_file,
    is_human_claude_prompt,
    spawned_agent_labels,
    spawned_agent_times,
    transcript_session_id,
    user_prompt_times_by_session,
)
from konvu_telemetry.pricing import cost_status, event_cost
from konvu_telemetry.service import (
    collect_forever,
    load_health,
    local_request_allowed,
    write_health,
)
from konvu_telemetry.snapshot import build_snapshot
from konvu_telemetry.storage import parse_timestamp, session_path, transcript_files


class ServiceTests(unittest.TestCase):
    def test_baseline_milestones_cover_long_sessions(self) -> None:
        self.assertEqual(
            BASELINE_MILESTONES,
            (
                10,
                20,
                30,
                40,
                50,
                60,
                70,
                80,
                90,
                100,
                150,
                200,
                250,
                300,
                350,
                400,
                500,
                600,
                700,
                800,
                900,
                1000,
            ),
        )

    def test_claude_stream_records_are_deduplicated(self) -> None:
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "session",
                "message": {
                    "id": "message",
                    "role": "assistant",
                    "model": "claude-test",
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "sessionId": "session",
                "message": {
                    "id": "message",
                    "role": "assistant",
                    "model": "claude-test",
                    "usage": {"input_tokens": 12, "output_tokens": 2},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(record) for record in records))
            events = list(events_in_file(transcript))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].usage.input_tokens, 12)
        self.assertEqual(events[0].usage.output_tokens, 2)

    def test_subagent_duplicate_wins_over_root_replay(self) -> None:
        usage = Usage(10, 1, 0, 0, 0, 0, "standard")
        root = UsageEvent(
            "claude",
            "session",
            "message",
            0,
            "model",
            usage,
            0,
            False,
            None,
            "standard",
        )
        subagent = UsageEvent(
            "claude",
            "session",
            "message",
            0,
            "model",
            usage,
            0,
            True,
            "agent",
            "standard",
        )
        self.assertEqual(deduplicate_usage_events([root, subagent]), [subagent])

    def test_codex_cumulative_checkpoints_become_deltas(self) -> None:
        def checkpoint(
            total: int, input_tokens: int, output_tokens: int
        ) -> dict[str, object]:
            return {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "total_tokens": total,
                            "input_tokens": input_tokens,
                            "cached_input_tokens": 0,
                            "cache_write_input_tokens": 0,
                            "output_tokens": output_tokens,
                            "reasoning_output_tokens": 0,
                        },
                        "last_token_usage": {
                            "input_tokens": input_tokens,
                            "cached_input_tokens": 0,
                            "cache_write_input_tokens": 0,
                            "output_tokens": output_tokens,
                            "reasoning_output_tokens": 0,
                        },
                    },
                },
            }

        with tempfile.TemporaryDirectory() as directory:
            transcript = (
                Path(directory) / "rollout-00000000-0000-0000-0000-000000000000.jsonl"
            )
            transcript.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [checkpoint(12, 10, 2), checkpoint(27, 12, 3)]
                )
            )
            events = list(codex_events_in_file(transcript))
        self.assertEqual([event.usage.total_tokens for event in events], [12, 15])

    def test_one_hour_claude_cache_write_uses_the_higher_rate(self) -> None:
        event = UsageEvent(
            provider="claude",
            session_id="session",
            message_id="message",
            timestamp=0,
            model="claude-test",
            usage=Usage(0, 0, 100, 100, 0, 0, "standard"),
            tool_calls=0,
            is_subagent=False,
            agent_id=None,
            effort="standard",
        )
        self.assertEqual(
            event_cost(
                event,
                {
                    "claude-test": {
                        "input": 1,
                        "output": 1,
                        "cache_write": 1,
                        "cache_read": 1,
                        "web_search": 0,
                        "fast_multiplier": 1,
                    }
                },
            ),
            160.0,
        )

    def test_claude_tool_results_do_not_create_prompt_boundaries(self) -> None:
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "session",
                "message": {"role": "user", "content": "real question"},
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "sessionId": "session",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "output"}],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(record) for record in records))
            prompts = user_prompt_times_by_session(transcript)
        self.assertEqual(len(prompts["session"]), 1)

    def test_claude_meta_compact_and_command_records_do_not_create_boundaries(
        self,
    ) -> None:
        real = {
            "timestamp": "2026-01-01T00:00:00Z",
            "sessionId": "session",
            "message": {"role": "user", "content": "real question"},
        }
        records = [
            real,
            {**real, "timestamp": "2026-01-01T00:00:01Z", "isMeta": True},
            {
                **real,
                "timestamp": "2026-01-01T00:00:02Z",
                "isCompactSummary": True,
            },
            {
                **real,
                "timestamp": "2026-01-01T00:00:03Z",
                "message": {
                    "role": "user",
                    "content": "<command-name>/compact</command-name>",
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(record) for record in records))
            self.assertEqual(
                user_prompt_times_by_session(transcript)["session"], [1767225600.0]
            )
        self.assertTrue(is_human_claude_prompt(real))

    def test_synthetic_claude_event_is_filtered_before_aggregation(self) -> None:
        record = {
            "timestamp": "2026-01-01T00:00:00Z",
            "sessionId": "session",
            "message": {
                "id": "synthetic",
                "role": "assistant",
                "model": "<synthetic>",
                "usage": {},
            },
        }
        self.assertIsNone(assistant_event(record))

    def test_cold_parser_skips_invalid_utf8_lines(self) -> None:
        record = {
            "timestamp": "2026-01-01T00:00:00Z",
            "sessionId": "session",
            "message": {
                "id": "message",
                "role": "assistant",
                "model": "claude-test",
                "usage": {"input_tokens": 1},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_bytes(b"\xff\n" + json.dumps(record).encode() + b"\n")
            self.assertEqual(len(list(events_in_file(transcript))), 1)

    def test_spawn_metadata_readers_skip_invalid_utf8_lines(self) -> None:
        record = {
            "timestamp": "2026-01-01T00:00:00Z",
            "sessionId": "session",
            "toolUseResult": {
                "agentId": "agent",
                "description": "Review tests",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_bytes(b"\xff\n" + json.dumps(record).encode() + b"\n")
            self.assertEqual(
                spawned_agent_times(transcript),
                {("session", "agent"): 1767225600.0},
            )
            self.assertEqual(
                spawned_agent_labels(transcript),
                {("session", "agent"): "Review tests"},
            )

    def test_long_context_and_fast_fallback_pricing_are_applied(self) -> None:
        prices = {
            "model": {
                "input": 1.0,
                "output": 1.0,
                "cache_write": 1.0,
                "cache_read": 1.0,
                "web_search": 0.0,
                "fast_multiplier": 2.0,
                "long_context_threshold": 200_000.0,
                "input_long": 3.0,
                "output_long": 3.0,
                "cache_write_long": 3.0,
                "cache_read_long": 3.0,
            }
        }
        long = UsageEvent(
            "claude",
            "session",
            "one",
            0,
            "model",
            Usage(200_001, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "standard",
        )
        fast = UsageEvent(
            "codex",
            "session",
            "two",
            0,
            "model",
            Usage(1, 0, 0, 0, 0, 0, "fast"),
            0,
            False,
            None,
            "standard",
        )
        self.assertEqual(event_cost(long, prices), 600003.0)
        self.assertEqual(event_cost(fast, prices), 2.0)

    def test_fast_priority_rates_are_not_multiplied_twice(self) -> None:
        prices = {
            "model": {
                "input": 1.0,
                "output": 1.0,
                "cache_write": 1.0,
                "cache_read": 1.0,
                "web_search": 0.0,
                "fast_multiplier": 2.0,
                "input_priority": 3.0,
                "output_priority": 3.0,
                "cache_write_priority": 3.0,
                "cache_read_priority": 3.0,
            }
        }
        event = UsageEvent(
            "codex",
            "session",
            "one",
            0,
            "model",
            Usage(1, 1, 0, 0, 0, 0, "fast"),
            0,
            False,
            None,
            "standard",
        )
        self.assertEqual(event_cost(event, prices), 6.0)

    def test_flex_rates_are_used_when_available(self) -> None:
        prices = {
            "model": {
                "input": 1.0,
                "output": 1.0,
                "cache_write": 1.0,
                "cache_read": 1.0,
                "web_search": 0.0,
                "fast_multiplier": 1.0,
                "input_flex": 0.5,
                "output_flex": 0.5,
                "cache_write_flex": 0.5,
                "cache_read_flex": 0.5,
            }
        }
        event = UsageEvent(
            "codex",
            "session",
            "one",
            0,
            "model",
            Usage(1, 1, 0, 0, 0, 0, "flex"),
            0,
            False,
            None,
            "standard",
        )
        self.assertEqual(event_cost(event, prices), 1.0)

    def test_naive_timestamps_are_consistently_utc(self) -> None:
        self.assertEqual(parse_timestamp("2026-01-01T00:00:00"), 1767225600.0)
        self.assertEqual(_timestamp("2026-01-01T00:00:00"), 1767225600.0)
        self.assertEqual(
            parse_timestamp("2026-01-01T00:00:00Z"),
            _timestamp("2026-01-01T00:00:00Z"),
        )

    def test_real_markup_prompt_remains_a_prompt(self) -> None:
        record = {"message": {"role": "user", "content": "<div>real user markup</div>"}}
        self.assertTrue(is_human_claude_prompt(record))
        self.assertTrue(is_claude_prompt(record))

    def test_cumulative_median_uses_the_actual_checkpoint_cohort(self) -> None:
        series = [
            [(1.0, 1)] * 250,
            [(2.0, 2)] * 250,
            [(3.0, 3)] * 250,
            [(100.0, 100)] * 10,
        ]
        points = cumulative_median_checkpoints(series)
        self.assertEqual(points[0]["sessions"], 4)
        self.assertEqual(points[-1]["sessions"], 3)
        self.assertEqual(points[0]["median_cost_usd"], 25.0)
        self.assertEqual(points[1]["median_cost_usd"], 40.0)

    def test_cumulative_median_never_decreases_when_cohort_changes(self) -> None:
        series = [
            *[[(100.0, 100)] * 10 for _ in range(3)],
            [(1.0, 1)] * 20,
            [(2.0, 2)] * 20,
            [(3.0, 3)] * 20,
        ]
        points = cumulative_median_checkpoints(series)
        self.assertEqual(points[0]["median_cost_usd"], 515.0)
        self.assertEqual(points[1]["median_cost_usd"], 515.0)
        self.assertEqual(points[0]["median_tokens"], 515)
        self.assertEqual(points[1]["median_tokens"], 515)

    def test_incremental_reader_keeps_large_prompt_boundary_without_retaining_text(
        self,
    ) -> None:
        prompt = {
            "type": "user",
            "message": {
                "role": "user",
                "content": "x" * 5_000_000 + '{"type":"tool_result"}',
            },
            "timestamp": "2026-01-01T00:00:00Z",
            "sessionId": "session",
        }
        first_response = {
            "timestamp": "2026-01-01T00:00:01Z",
            "sessionId": "session",
            "message": {
                "id": "first",
                "role": "assistant",
                "model": "claude-test",
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        }
        second_response = {
            "timestamp": "2026-01-01T00:00:02Z",
            "sessionId": "session",
            "message": {
                "id": "second",
                "role": "assistant",
                "model": "claude-test",
                "usage": {"input_tokens": 12, "output_tokens": 2},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text(
                "\n".join(json.dumps(record) for record in [prompt, first_response])
                + "\n"
            )
            state = IncrementalLiveState()
            first = state._refresh_claude(transcript)
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(second_response) + "\n")
            second = state._refresh_claude(transcript)
        self.assertEqual(first.prompts["session"], [1767225600.0])
        self.assertEqual(len(second.events), 2)

    def test_task_series_preserves_empty_prompts(self) -> None:
        event = UsageEvent(
            "claude",
            "session",
            "message",
            2,
            "claude-test",
            Usage(3, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "standard",
        )
        prices = {
            "claude-test": {
                "input": 1,
                "output": 1,
                "cache_write": 1,
                "cache_read": 1,
                "web_search": 0,
            }
        }
        self.assertEqual(task_series([event], [1, 3], prices), [(3.0, 3), (0.0, 0)])

    def test_task_series_rejects_unknown_billable_prices(self) -> None:
        event = UsageEvent(
            "claude",
            "session",
            "message",
            2,
            "unknown",
            Usage(3, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "standard",
        )
        self.assertIsNone(task_series([event], [1], {}))

    def test_task_series_attributes_priced_children_but_ignores_internal_review(
        self,
    ) -> None:
        parent = UsageEvent(
            "codex",
            "session",
            "parent",
            2,
            "model",
            Usage(1, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "medium",
        )
        child = UsageEvent(
            "codex",
            "session",
            "child",
            3,
            "model",
            Usage(2, 0, 0, 0, 0, 0, "standard"),
            0,
            True,
            "child",
            "medium",
        )
        review = UsageEvent(
            "codex",
            "session",
            "review",
            4,
            "codex-auto-review",
            Usage(10, 0, 0, 0, 0, 0, "standard"),
            0,
            True,
            "review",
            "low",
        )
        prices = {
            "model": {
                "input": 1,
                "output": 1,
                "cache_write": 1,
                "cache_read": 1,
                "web_search": 0,
            }
        }
        self.assertEqual(task_series([parent, child, review], [1], prices), [(3.0, 3)])

    def test_health_becomes_stale_after_collector_deadline(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "konvu_telemetry.service.health_path",
                return_value=Path(directory) / "health.json",
            ),
        ):
            write_health(100.0, interval_seconds=60)
            self.assertEqual(load_health(200.0)["status"], "healthy")
            health = load_health(251.0)
        self.assertEqual(health["status"], "stale")
        self.assertEqual(health["stale_for_seconds"], 151)

    def test_collector_keeps_polling_when_health_state_cannot_be_written(self) -> None:
        with (
            patch(
                "konvu_telemetry.service.build_snapshot", side_effect=OSError("full")
            ),
            patch("konvu_telemetry.service.write_health", side_effect=OSError("full")),
            patch("konvu_telemetry.service.time.sleep", side_effect=StopIteration),
            self.assertLogs("konvu_telemetry.service", level="ERROR"),
            self.assertRaises(StopIteration),
        ):
            collect_forever(60, IncrementalLiveState(), Lock())

    def test_dashboard_rejects_non_local_or_malformed_origins(self) -> None:
        self.assertTrue(local_request_allowed("127.0.0.1:7824", None))
        self.assertTrue(
            local_request_allowed("localhost:7824", "http://localhost:7824")
        )
        self.assertFalse(local_request_allowed("evil.example:7824", None))
        self.assertFalse(local_request_allowed("127.0.0.1:7824", "null"))
        self.assertFalse(
            local_request_allowed("127.0.0.1:7824", "file:///tmp/dashboard.html")
        )

    def test_session_paths_reject_path_traversal(self) -> None:
        for session_id in ("../../outside", "<img src=x onerror=alert(1)>", ""):
            with self.subTest(session_id=session_id), self.assertRaises(ValueError):
                session_path("claude", session_id)

    def test_malformed_transcript_session_ids_are_ignored(self) -> None:
        record = {
            "timestamp": "2026-01-01T00:00:00Z",
            "sessionId": "<img src=x onerror=alert(1)>",
            "message": {"role": "assistant", "model": "claude-test", "usage": {}},
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text(json.dumps(record))
            self.assertIsNone(transcript_session_id(transcript))
        self.assertIsNone(assistant_event(record))

    def test_unknown_model_marks_a_session_cost_unavailable(self) -> None:
        event = UsageEvent(
            "claude",
            "session",
            "message",
            0,
            "unknown-model",
            Usage(1, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "standard",
        )
        self.assertEqual(cost_status([event], {}), ("unavailable", 1))

    def test_internal_codex_review_does_not_hide_priced_parent_spend(self) -> None:
        parent = UsageEvent(
            "codex",
            "session",
            "message",
            0,
            "gpt-5.6-terra",
            Usage(1, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "medium",
        )
        review = UsageEvent(
            "codex",
            "session",
            "review",
            0,
            "codex-auto-review",
            Usage(1, 0, 0, 0, 0, 0, "standard"),
            0,
            True,
            "guardian",
            "low",
        )
        prices = {
            "gpt-5.6-terra": {
                "input": 1,
                "output": 1,
                "cache_write": 1,
                "cache_read": 1,
                "web_search": 0,
                "fast_multiplier": 1,
            }
        }
        self.assertEqual(cost_status([parent, review], prices), ("complete", 0))

    def test_zero_usage_synthetic_event_does_not_hide_priced_session(self) -> None:
        priced = UsageEvent(
            "claude",
            "session",
            "message",
            0,
            "claude-test",
            Usage(1, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "standard",
        )
        synthetic = UsageEvent(
            "claude",
            "session",
            "synthetic",
            1,
            "<synthetic>",
            Usage(0, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "standard",
        )
        prices = {
            "claude-test": {
                "input": 1,
                "output": 1,
                "cache_write": 1,
                "cache_read": 1,
                "web_search": 0,
                "fast_multiplier": 1,
            }
        }
        self.assertEqual(cost_status([priced, synthetic], prices), ("complete", 0))

    def test_transcript_discovery_rejects_symlinks_outside_configured_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "logs"
            root.mkdir()
            outside = Path(directory) / "outside.jsonl"
            outside.write_text("{}\n")
            (root / "escape.jsonl").symlink_to(outside)
            self.assertEqual(list(transcript_files(root)), [])

    def test_cold_refresh_loads_old_codex_children_of_a_live_parent(self) -> None:
        parent_id = "00000000-0000-0000-0000-000000000001"
        child_id = "00000000-0000-0000-0000-000000000002"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / f"rollout-{parent_id}.jsonl"
            child = root / f"rollout-{child_id}.jsonl"
            parent.write_text("{}\n")
            child.write_text("{}\n")
            now = time.time()
            os.utime(child, (now - 48 * 60 * 60, now - 48 * 60 * 60))
            state = IncrementalLiveState()

            def parse(path: Path) -> CodexLiveFile:
                item = CodexLiveFile(
                    session_id=parent_id if path == parent else child_id
                )
                item.parent = (parent_id, "child") if path == child else None
                state.codex[path] = item
                return item

            with (
                patch("konvu_telemetry.live.codex_roots", return_value=[root]),
                patch(
                    "konvu_telemetry.live.transcript_files",
                    side_effect=lambda _root: iter([parent, child]),
                ),
                patch(
                    "konvu_telemetry.live.codex_subagent_parent",
                    side_effect=lambda path: (
                        (parent_id, "child") if path == child else None
                    ),
                ),
                patch.object(state, "_refresh_codex", side_effect=parse),
            ):
                _, paths = state.refresh(now)
            self.assertIn(child, paths)

    def test_first_post_compact_forecast_scales_precompact_trend(self) -> None:
        self.assertEqual(scaled_precompact_forecast([4.0, 6.0], 50, 100), 25.0)

    def test_baseline_comparison_interpolates_and_extrapolates_exact_task_counts(
        self,
    ) -> None:
        baseline = {
            "providers": {
                "claude": [
                    {
                        "iterations": 10,
                        "sessions": 8,
                        "median_cost_usd": 10.0,
                        "median_tokens": 100,
                    },
                    {
                        "iterations": 30,
                        "sessions": 6,
                        "median_cost_usd": 40.0,
                        "median_tokens": 400,
                    },
                    {
                        "iterations": 100,
                        "sessions": 4,
                        "median_cost_usd": 180.0,
                        "median_tokens": 1800,
                    },
                ],
            },
            "configurations": {},
        }
        interpolated = baseline_comparison("claude", 20, 500, baseline, cost_usd=25.0)
        extrapolated = baseline_comparison(
            "claude", 120, 2500, baseline, cost_usd=220.0
        )
        self.assertEqual(interpolated["iterations"], 20)
        self.assertEqual(interpolated["median_tokens"], 250)
        self.assertEqual(interpolated["median_cost_usd"], 25.0)
        self.assertEqual(interpolated["cost_overhead_percent"], 0)
        self.assertEqual(extrapolated["iterations"], 120)
        self.assertEqual(extrapolated["median_tokens"], 2200)
        self.assertEqual(extrapolated["median_cost_usd"], 220.0)
        self.assertEqual(extrapolated["cost_overhead_percent"], 0)

    def test_baseline_text_reports_spend_when_priced_cost_is_available(self) -> None:
        text = baseline_text(
            {
                "baseline": {
                    "emoji": "🔴",
                    "iterations": 325,
                    "token_overhead_percent": 9,
                    "cost_overhead_percent": 117,
                }
            }
        )
        self.assertEqual(text, "🔴 117% over your median")

    def test_task_series_preserves_zero_cost_prompt_boundaries(self) -> None:
        prices = {
            "model": {
                "input": 1,
                "output": 1,
                "cache_write": 1,
                "cache_read": 1,
                "web_search": 0,
                "fast_multiplier": 1,
            }
        }
        first = UsageEvent(
            "claude",
            "session",
            "one",
            1,
            "model",
            Usage(1, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "medium",
        )
        third = UsageEvent(
            "claude",
            "session",
            "three",
            3,
            "model",
            Usage(2, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "medium",
        )
        self.assertEqual(
            task_series([first, third], [1, 2, 3], prices),
            [(1.0, 1), (0.0, 0), (2.0, 2)],
        )

    def test_configuration_baseline_never_mislabels_mixed_or_fast_work(self) -> None:
        standard = UsageEvent(
            "codex",
            "session",
            "one",
            1,
            "model",
            Usage(1, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "medium",
        )
        fast = UsageEvent(
            "codex",
            "session",
            "two",
            2,
            "model",
            Usage(1, 0, 0, 0, 0, 0, "fast"),
            0,
            False,
            None,
            "medium",
        )
        self.assertIsNone(single_configuration([standard, fast]))
        series = [(1.0, 1)] * 10
        samples = [
            ("model", "medium", "standard", series),
            ("model", "medium", "standard", series),
            ("model", "medium", "standard", series),
            (None, None, None, series),
        ]
        with (
            patch(
                "konvu_telemetry.analytics.historical_task_series",
                side_effect=lambda provider, _since, _prices: iter(
                    samples if provider == "codex" else []
                ),
            ),
            patch(
                "konvu_telemetry.analytics.claude_compact_next_ten_costs",
                return_value=[],
            ),
        ):
            baseline = build_baselines(0, {})
        matched = baseline_comparison(
            "codex", 10, 10, baseline, "model", "medium", "standard", cost_usd=10
        )
        wrong_speed = baseline_comparison(
            "codex", 10, 10, baseline, "model", "medium", "fast", cost_usd=10
        )
        self.assertEqual(matched["scope"], "model_effort_speed")
        self.assertEqual(matched["speed"], "standard")
        self.assertEqual(wrong_speed["scope"], "provider")

    def test_snapshot_exposes_only_uniform_comparison_configuration(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": session_id,
                "message": {"role": "user", "content": "question"},
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "sessionId": session_id,
                "effort": "medium",
                "message": {
                    "id": "message",
                    "role": "assistant",
                    "model": "model",
                    "usage": {"input_tokens": 1, "speed": "fast"},
                },
            },
        ]
        prices = {
            "model": {
                "input": 1,
                "output": 1,
                "cache_write": 1,
                "cache_read": 1,
                "web_search": 0,
                "fast_multiplier": 1,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{session_id}.jsonl"
            transcript.write_text("\n".join(json.dumps(record) for record in records))
            with (
                patch(
                    "konvu_telemetry.snapshot.live_transcripts",
                    return_value=[transcript],
                ),
                patch(
                    "konvu_telemetry.snapshot.live_codex_transcripts", return_value=[]
                ),
                patch("konvu_telemetry.snapshot.load_pricing", return_value=prices),
                patch(
                    "konvu_telemetry.snapshot.load_baselines",
                    return_value={"providers": {}, "configurations": {}},
                ),
                patch("konvu_telemetry.snapshot.pinned_sessions", return_value=[]),
                patch("konvu_telemetry.snapshot.enrich_snapshot"),
                patch("konvu_telemetry.snapshot.locate_compactions"),
                patch("konvu_telemetry.snapshot.apply_notification_tracking"),
            ):
                snapshot = build_snapshot(1767225602.0)
        session = snapshot["sessions"][0]
        self.assertEqual(session["speed"], "fast")
        self.assertEqual(
            session["comparison_configuration"],
            {"model": "model", "effort": "medium", "speed": "fast"},
        )

    def test_linear_priced_spend_at_nearby_task_counts_never_alerts(self) -> None:
        baseline = {
            "providers": {
                "claude": [
                    {
                        "iterations": 10,
                        "sessions": 4,
                        "median_cost_usd": 10.0,
                        "median_tokens": 100,
                    },
                    {
                        "iterations": 30,
                        "sessions": 4,
                        "median_cost_usd": 40.0,
                        "median_tokens": 400,
                    },
                ]
            },
            "configurations": {},
        }
        sessions = [
            {
                "id": str(count),
                "provider": "claude",
                "total_cost_usd": cost,
                "cost_status": "complete",
                "baseline": baseline_comparison(
                    "claude", count, tokens, baseline, cost_usd=cost
                ),
            }
            for count, tokens, cost in ((20, 500, 25.0), (21, 530, 26.5))
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "konvu_telemetry.analytics.notification_state_path",
                return_value=Path(directory) / "notifications.json",
            ):
                apply_notification_tracking(sessions, 100.0)
        self.assertEqual(
            [session["notification"]["hot"] for session in sessions], [False, False]
        )

    def test_partial_cost_session_does_not_trigger_hot_alert(self) -> None:
        sessions = [
            {
                "id": "session",
                "provider": "claude",
                "total_cost_usd": 12.0,
                "cost_status": "partial",
                "baseline": {"token_overhead_percent": 150},
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "notifications.json"
            with patch(
                "konvu_telemetry.analytics.notification_state_path",
                return_value=state_path,
            ):
                apply_notification_tracking(sessions, 100.0)
        self.assertEqual(sessions[0]["notification"], {"sequence": 0, "hot": False})

    def test_alerts_require_high_overhead_and_forecast_then_wait_ten_minutes(
        self,
    ) -> None:
        session = {
            "id": "session",
            "provider": "claude",
            "total_cost_usd": 10.0,
            "projected_next_10_tasks_usd": 4.01,
            "cost_status": "complete",
            "baseline": {"cost_overhead_percent": 200},
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "notifications.json"
            with patch(
                "konvu_telemetry.analytics.notification_state_path",
                return_value=state_path,
            ):
                apply_notification_tracking([session], 100.0)
                self.assertEqual(session["notification"]["sequence"], 1)
                session["total_cost_usd"] = 11.0
                session["baseline"] = {"cost_overhead_percent": 210}
                apply_notification_tracking([session], 100.0 + 9 * 60)
                self.assertEqual(session["notification"]["sequence"], 1)
                apply_notification_tracking([session], 100.0 + 10 * 60)
        self.assertEqual(session["notification"]["sequence"], 2)


if __name__ == "__main__":
    unittest.main()
