import json
from datetime import datetime, timezone
import os
import sys
import tempfile
from threading import Lock
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scripts.update_pricing import validated_payload

from konvu_telemetry.analytics import (
    apply_notification_tracking,
    baseline_comparison,
    build_baselines,
    cumulative_median_checkpoints,
    deduplicate_usage_events,
    forecast_backtest_sample,
    load_baselines,
    scaled_precompact_forecast,
    single_configuration,
    task_series,
)
from konvu_telemetry.config import (
    BASELINE_MILESTONES,
    BASELINE_MIN_SESSIONS,
    BASELINE_SCHEMA_VERSION,
)
from konvu_telemetry.display import (
    baseline_text,
    quota_usage_text,
    record_claude_quotas,
    refreshed_session,
)
from konvu_telemetry.fleet_telemetry import (
    _CACHE as TELEMETRY_CACHE,
    TranscriptTelemetry,
    _comparable_forecast,
    _timestamp,
    enrich_snapshot,
    is_claude_prompt,
    parse_telemetry,
)
from konvu_telemetry.live import CodexLiveFile, IncrementalLiveState
from konvu_telemetry.models import Usage, UsageEvent
from konvu_telemetry.parsers import (
    assistant_event,
    codex_events_in_file,
    codex_subagent_parent,
    events_in_file,
    is_human_claude_prompt,
    spawned_agent_labels,
    spawned_agent_times,
    transcript_session_id,
    user_prompt_times_by_session,
)
from konvu_telemetry.pricing import cost_status, event_cost, load_pricing
from konvu_telemetry.service import (
    DashboardRequestHandler,
    collect_forever,
    load_health,
    local_request_allowed,
    write_health,
)
from konvu_telemetry.snapshot import (
    build_snapshot,
    sampled_rows,
    sampled_rows_with_tail,
    summary_snapshot,
    write_snapshot,
)
from konvu_telemetry.storage import (
    parse_timestamp,
    session_path,
    transcript_files,
    write_private_json_if_changed,
)


class ServiceTests(unittest.TestCase):
    def test_pricing_update_rejects_boolean_required_rates(self) -> None:
        payload: dict[str, object] = {f"model-{index}": {} for index in range(1_000)}
        payload["claude-sonnet-4-6"] = {
            "input_cost_per_token": True,
            "output_cost_per_token": 1,
        }
        payload["gpt-5.4"] = {
            "input_cost_per_token": 1,
            "output_cost_per_token": 1,
        }
        with self.assertRaises(ValueError):
            validated_payload(json.dumps(payload).encode())

    def test_previous_baseline_schema_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline_file = Path(directory) / "baselines.json"
            baseline_file.write_text(
                json.dumps(
                    {
                        "schema_version": 8,
                        "generated_at": "2026-09-21T00:00:00Z",
                        "milestones": list(BASELINE_MILESTONES),
                        "median_method": "monotonic_checkpoint_cohort_medians",
                        "providers": {},
                        "configurations": {},
                        "forecasts": {},
                    }
                )
            )
            replacement = {"schema_version": 9, "providers": {}}
            with (
                patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
                patch(
                    "konvu_telemetry.analytics.build_baselines",
                    return_value=replacement,
                ) as builder,
            ):
                loaded = load_baselines(1_767_225_700, {})
        self.assertEqual(loaded, replacement)
        builder.assert_called_once()

    def test_pricing_is_cached_until_the_source_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pricing_path = Path(directory) / "pricing.json"
            pricing_path.write_text(
                json.dumps(
                    {
                        "model": {
                            "input_cost_per_token": 1,
                            "output_cost_per_token": 2,
                        }
                    }
                )
            )
            with patch.dict(
                os.environ, {"KONVU_TELEMETRY_PRICING_PATH": str(pricing_path)}
            ):
                first = load_pricing()
                second = load_pricing()
                self.assertIs(first, second)
                pricing_path.write_text(
                    json.dumps(
                        {
                            "model": {
                                "input_cost_per_token": 3,
                                "output_cost_per_token": 4,
                            }
                        }
                    )
                )
                refreshed = load_pricing()
        self.assertIsNot(first, refreshed)
        self.assertEqual(refreshed["model"]["input"], 3)

    def test_invalid_price_rates_are_not_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pricing_path = Path(directory) / "pricing.json"
            pricing_path.write_text(
                json.dumps(
                    {
                        "negative": {
                            "input_cost_per_token": -1,
                            "output_cost_per_token": 2,
                        },
                        "not-finite": {
                            "input_cost_per_token": float("nan"),
                            "output_cost_per_token": 2,
                        },
                        "valid": {
                            "input_cost_per_token": 1,
                            "output_cost_per_token": 2,
                        },
                    }
                )
            )
            with patch.dict(
                os.environ, {"KONVU_TELEMETRY_PRICING_PATH": str(pricing_path)}
            ):
                prices = load_pricing()
        self.assertEqual(set(prices), {"valid"})

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

    def test_workflow_child_prompt_labels_a_claude_subagent(self) -> None:
        record = {
            "sessionId": "session",
            "agentId": "agent",
            "isSidechain": True,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "user",
                "content": "Review the release checklist and report any blockers.",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "agent-agent.jsonl"
            transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")
            self.assertEqual(
                spawned_agent_labels(transcript),
                {
                    ("session", "agent"): (
                        "Review the release checklist and report any blockers."
                    )
                },
            )
            self.assertEqual(
                spawned_agent_times(transcript),
                {("session", "agent"): 1767225600.0},
            )

    def test_codex_subagent_prefers_nickname_over_technical_path(self) -> None:
        parent_id = "00000000-0000-0000-0000-000000000001"
        child_id = "00000000-0000-0000-0000-000000000002"
        record = {
            "type": "session_meta",
            "payload": {
                "id": child_id,
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "agent_path": "/root/greptile_583",
                            "agent_nickname": "Hubble",
                        }
                    }
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{child_id}.jsonl"
            transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")
            self.assertEqual(codex_subagent_parent(transcript), (parent_id, "Hubble"))

    def test_codex_child_activity_keeps_its_parent_session_live(self) -> None:
        parent_id = "00000000-0000-0000-0000-000000000001"
        child_id = "00000000-0000-0000-0000-000000000002"

        def token_count(timestamp: str) -> dict[str, object]:
            return {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "model": "model",
                        "total_token_usage": {
                            "total_tokens": 1,
                            "input_tokens": 1,
                            "cached_input_tokens": 0,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 0,
                        },
                        "last_token_usage": {
                            "input_tokens": 1,
                            "cached_input_tokens": 0,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 0,
                        },
                    },
                },
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / f"rollout-{parent_id}.jsonl"
            child = root / f"rollout-{child_id}.jsonl"
            parent.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        {
                            "type": "session_meta",
                            "payload": {"id": parent_id, "source": "cli"},
                        },
                        {
                            "timestamp": "2026-01-01T00:00:00Z",
                            "type": "event_msg",
                            "payload": {"type": "task_started"},
                        },
                        token_count("2026-01-01T00:00:01Z"),
                    ]
                ),
                encoding="utf-8",
            )
            child.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": child_id,
                                "source": {
                                    "subagent": {
                                        "thread_spawn": {
                                            "parent_thread_id": parent_id,
                                            "agent_nickname": "Hubble",
                                        }
                                    }
                                },
                            },
                        },
                        token_count("2026-01-01T00:00:20Z"),
                    ]
                ),
                encoding="utf-8",
            )
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
            with (
                patch("konvu_telemetry.snapshot.live_transcripts", return_value=[]),
                patch(
                    "konvu_telemetry.snapshot.live_codex_transcripts",
                    return_value=[parent, child],
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
                snapshot = build_snapshot(1767225630.0)
        session = snapshot["sessions"][0]
        self.assertEqual(session["last_activity_at"], "2026-01-01T00:00:20+00:00")
        self.assertEqual(session["subagents"][0]["label"], "Hubble")
        self.assertEqual(session["subagents"][0]["cost_usd"], 1.0)
        self.assertEqual(session["subagent_cost_usd"], 1.0)
        self.assertEqual(session["total_cost_usd"], 2.0)

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
            [(4.0, 4)] * 250,
            [(5.0, 5)] * 250,
            [(100.0, 100)] * 10,
        ]
        points = cumulative_median_checkpoints(series)
        self.assertEqual(points[0]["sessions"], 6)
        self.assertEqual(points[-1]["sessions"], 5)
        self.assertEqual(points[0]["median_cost_usd"], 35.0)
        self.assertEqual(points[1]["median_cost_usd"], 60.0)

    def test_cumulative_median_never_decreases_when_cohort_changes(self) -> None:
        series = [
            *[[(100.0, 100)] * 10 for _ in range(5)],
            [(1.0, 1)] * 20,
            [(2.0, 2)] * 20,
            [(3.0, 3)] * 20,
            [(4.0, 4)] * 20,
            [(5.0, 5)] * 20,
        ]
        points = cumulative_median_checkpoints(series)
        self.assertEqual(points[0]["median_cost_usd"], 525.0)
        self.assertEqual(points[1]["median_cost_usd"], 525.0)
        self.assertEqual(points[0]["median_tokens"], 525)
        self.assertEqual(points[1]["median_tokens"], 525)

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

    def test_fleet_telemetry_incrementally_reads_bounded_appends(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        prompt = {
            "type": "user",
            "message": {"role": "user", "content": "x" * 1_000_000},
            "timestamp": "2026-01-01T00:00:00Z",
            "sessionId": session_id,
        }
        completion = {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": "2026-01-01T00:00:01Z",
            "sessionId": session_id,
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{session_id}.jsonl"
            transcript.write_text(json.dumps(prompt) + "\n")
            TELEMETRY_CACHE.clear()
            first = parse_telemetry(transcript, "claude")
            self.assertEqual(first.activity, [(1767225600.0, "running", None)])
            first_offset = TELEMETRY_CACHE[("claude", str(transcript))].offset
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(completion) + "\n")
            second = parse_telemetry(transcript, "claude")
        self.assertIs(first, second)
        self.assertEqual(second.activity[-1], (1767225601.0, "idle", None))
        self.assertEqual(second.completions, [(1767225601.0, None)])
        self.assertGreater(
            TELEMETRY_CACHE[("claude", str(transcript))].offset, first_offset
        )

    def test_fleet_telemetry_waits_for_complete_appended_record(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        record = json.dumps(
            {
                "type": "system",
                "subtype": "turn_duration",
                "timestamp": "2026-01-01T00:00:01Z",
                "sessionId": session_id,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{session_id}.jsonl"
            transcript.write_text(record[:20])
            TELEMETRY_CACHE.clear()
            first = parse_telemetry(transcript, "claude")
            self.assertEqual(first.completions, [])
            self.assertEqual(TELEMETRY_CACHE[("claude", str(transcript))].offset, 0)
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(record[20:] + "\n")
            second = parse_telemetry(transcript, "claude")
        self.assertIs(first, second)
        self.assertEqual(second.completions, [(1767225601.0, None)])

    def test_fleet_telemetry_resets_after_truncation(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        first_record = {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": "2026-01-01T00:00:01Z",
            "sessionId": session_id,
            "padding": "x" * 100,
        }
        second_record = {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": "2026-01-01T00:00:02Z",
            "sessionId": session_id,
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{session_id}.jsonl"
            transcript.write_text(json.dumps(first_record) + "\n")
            TELEMETRY_CACHE.clear()
            first = parse_telemetry(transcript, "claude")
            transcript.write_text(json.dumps(second_record) + "\n")
            second = parse_telemetry(transcript, "claude")
        self.assertIsNot(first, second)
        self.assertEqual(second.completions, [(1767225602.0, None)])

    def test_fleet_telemetry_resets_when_rewrite_stays_above_read_offset(
        self,
    ) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        old_record = {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": "2026-01-01T00:00:01Z",
            "sessionId": session_id,
        }
        new_record = {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": "2026-01-01T00:00:02Z",
            "sessionId": session_id,
            "padding": "y" * 300,
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{session_id}.jsonl"
            transcript.write_bytes(
                (json.dumps(old_record) + "\n").encode() + b"x" * 1_000
            )
            TELEMETRY_CACHE.clear()
            first = parse_telemetry(transcript, "claude")
            cached = TELEMETRY_CACHE[("claude", str(transcript))]
            self.assertGreater(cached.size, cached.offset)
            transcript.write_text(json.dumps(new_record) + "\n")
            self.assertGreater(transcript.stat().st_size, cached.offset)
            second = parse_telemetry(transcript, "claude")
        self.assertIsNot(first, second)
        self.assertEqual(second.completions, [(1767225602.0, None)])

    def test_snapshot_summary_bounds_history_and_keeps_endpoints(self) -> None:
        rows = [{"iteration": index} for index in range(1_000)]
        sampled = sampled_rows(rows, 40)
        recent = sampled_rows_with_tail(rows, 40, 6)
        summary = summary_snapshot(
            {
                "generated_at": "2026-01-01T00:20:00Z",
                "sessions": [
                    {
                        "last_activity_at": "2026-01-01T00:10:00Z",
                        "iterations": rows,
                        "context_history": rows,
                        "subagents": rows,
                    }
                ],
            }
        )
        session = summary["sessions"][0]
        self.assertEqual(len(sampled), 40)
        self.assertEqual((sampled[0], sampled[-1]), (rows[0], rows[-1]))
        self.assertEqual(recent[-6:], rows[-6:])
        self.assertEqual(len(session["iterations"]), 40)
        self.assertNotIn("context_history", session)
        self.assertNotIn("subagents", session)

    def test_snapshot_summary_excludes_sessions_outside_dashboard_window(self) -> None:
        summary = summary_snapshot(
            {
                "generated_at": "2026-01-01T00:20:01Z",
                "sessions": [
                    {"id": "recent", "last_activity_at": "2026-01-01T00:00:01Z"},
                    {"id": "stale", "last_activity_at": "2026-01-01T00:00:00Z"},
                ],
            }
        )
        self.assertEqual([session["id"] for session in summary["sessions"]], ["recent"])

    def test_snapshot_is_not_published_when_detail_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "live-sessions.json"
            destination.write_text('{"old":true}')
            with (
                patch(
                    "konvu_telemetry.snapshot.snapshot_path",
                    return_value=destination,
                ),
                patch(
                    "konvu_telemetry.snapshot.session_path",
                    return_value=Path(directory) / "session.json",
                ),
                patch(
                    "konvu_telemetry.snapshot.write_private_json_if_changed",
                    side_effect=OSError("full"),
                ),
            ):
                with self.assertRaises(OSError):
                    write_snapshot(
                        {
                            "generated_at": "2026-01-01T00:00:00Z",
                            "sessions": [{"provider": "claude", "id": "session"}],
                        }
                    )
            self.assertEqual(destination.read_text(), '{"old":true}')

    def test_private_json_skips_unchanged_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "state.json"
            self.assertTrue(write_private_json_if_changed(destination, {"a": 1}))
            inode = destination.stat().st_ino
            self.assertFalse(write_private_json_if_changed(destination, {"a": 1}))
            self.assertEqual(destination.stat().st_ino, inode)
            self.assertTrue(write_private_json_if_changed(destination, {"a": 2}))

    def test_stale_valid_baseline_refreshes_without_blocking(self) -> None:
        old = {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "milestones": list(BASELINE_MILESTONES),
            "minimum_sessions": BASELINE_MIN_SESSIONS,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "forecasts": {},
            "configurations": {},
            "median_method": "monotonic_checkpoint_cohort_medians",
        }
        new = {**old, "generated_at": "2026-01-02T00:00:00+00:00"}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "baselines.json"
            destination.write_text(json.dumps(old))
            with (
                patch(
                    "konvu_telemetry.analytics.baseline_path",
                    return_value=destination,
                ),
                patch("konvu_telemetry.analytics.build_baselines", return_value=new),
                patch("konvu_telemetry.analytics.Thread") as thread_class,
            ):
                thread_class.return_value.start.side_effect = lambda: (
                    thread_class.call_args.kwargs["target"](
                        *thread_class.call_args.kwargs["args"]
                    )
                )
                loaded = load_baselines(1767312000.0, {}, refresh_in_background=True)
                self.assertEqual(loaded, old)
                self.assertEqual(json.loads(destination.read_text()), new)
                thread_class.assert_called_once()

    def test_failed_baseline_thread_start_does_not_hold_refresh_lock(self) -> None:
        baseline = {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "milestones": list(BASELINE_MILESTONES),
            "minimum_sessions": BASELINE_MIN_SESSIONS,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "forecasts": {},
            "configurations": {},
            "median_method": "monotonic_checkpoint_cohort_medians",
        }
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "baselines.json"
            destination.write_text(json.dumps(baseline))
            with (
                patch(
                    "konvu_telemetry.analytics.baseline_path",
                    return_value=destination,
                ),
                patch("konvu_telemetry.analytics.Thread") as thread_class,
                patch("konvu_telemetry.analytics.LOGGER.exception"),
            ):
                thread_class.return_value.start.side_effect = RuntimeError("limited")
                first = load_baselines(1767312000.0, {}, refresh_in_background=True)
                second = load_baselines(1767312000.0, {}, refresh_in_background=True)
        self.assertEqual((first, second), (baseline, baseline))
        self.assertEqual(thread_class.call_count, 2)

    def test_dashboard_json_uses_etags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "live-sessions.json"
            snapshot.write_text('{"generated_at":"2026-01-01T00:00:00Z","sessions":[]}')
            handler = Mock()
            handler.headers = {}
            DashboardRequestHandler._serve_json_file(handler, snapshot)
            etag = f'"{snapshot.stat().st_mtime_ns:x}-{snapshot.stat().st_size:x}"'
            handler.send_response.assert_called_once_with(200)
            handler.send_header.assert_any_call("ETag", etag)
            handler._write_payload.assert_called_once_with(snapshot.read_bytes())

            handler.reset_mock()
            handler.headers = {"If-None-Match": etag}
            DashboardRequestHandler._serve_json_file(handler, snapshot)
            handler.send_response.assert_called_once_with(304)
            handler._write_payload.assert_not_called()

    def test_refreshed_session_uses_fresh_collector_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            health = root / "health.json"
            session = root / "session.json"
            health.write_text(
                json.dumps(
                    {
                        "status": "healthy",
                        "last_success_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            )
            session.write_text('{"id":"session"}')
            with (
                patch("konvu_telemetry.display.health_path", return_value=health),
                patch("konvu_telemetry.display.session_path", return_value=session),
                patch("konvu_telemetry.display.urlopen") as request,
            ):
                payload = refreshed_session(
                    "claude", "00000000-0000-0000-0000-000000000001"
                )
        self.assertEqual(payload, {"id": "session"})
        request.assert_not_called()

    def test_refreshed_session_requests_refresh_when_collector_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            health = root / "health.json"
            session = root / "session.json"
            health.write_text(
                json.dumps(
                    {
                        "status": "healthy",
                        "last_success_at": "2026-01-01T00:00:00+00:00",
                    }
                )
            )
            session.write_text('{"id":"fallback"}')
            with (
                patch("konvu_telemetry.display.health_path", return_value=health),
                patch("konvu_telemetry.display.session_path", return_value=session),
                patch(
                    "konvu_telemetry.display.urlopen", side_effect=OSError("offline")
                ) as request,
            ):
                payload = refreshed_session(
                    "claude", "00000000-0000-0000-0000-000000000001"
                )
        self.assertEqual(payload, {"id": "fallback"})
        request.assert_called_once()

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
        self.assertEqual(scaled_precompact_forecast([4.0, 6.0, 5.0], 50, 100), 25.0)
        self.assertIsNone(scaled_precompact_forecast([4.0, 6.0], 50, 100))

    def test_precompact_forecast_basis_is_labeled_separately(self) -> None:
        session: dict[str, object] = {
            "projected_next_10_tasks_usd": 25.0,
            "compact_events": [{"timestamp": "2026-01-01T00:00:10Z"}],
            "iterations": [
                {"started_at": "2026-01-01T00:00:01Z", "priced": True},
                {"started_at": "2026-01-01T00:00:02Z", "priced": True},
                {"started_at": "2026-01-01T00:00:11Z", "priced": True},
            ],
        }
        _comparable_forecast(session, [], False)
        self.assertEqual(session["projected_next_10_tasks_usd"], 25.0)
        self.assertEqual(
            session["forecast_basis"]["method"], "context_scaled_precompact"
        )
        self.assertEqual(session["forecast_basis"]["sample_count"], 2)

    def test_forecast_backtest_holds_out_the_final_ten_tasks(self) -> None:
        self.assertEqual(forecast_backtest_sample([(1.0, 1)] * 20), (10.0, 10.0))
        self.assertIsNone(forecast_backtest_sample([(1.0, 1)] * 19))

    def test_forecast_uses_completed_matching_configuration_only(self) -> None:
        session: dict[str, object] = {
            "speed": "standard",
            "iterations": [
                {
                    "started_at": f"2026-01-01T00:00:0{index}Z",
                    "cost_usd": float(index + 1),
                    "priced": True,
                    "speed": "standard",
                }
                for index in range(4)
            ],
        }
        telemetry = TranscriptTelemetry(
            configurations=[(float(index), "model", "medium") for index in range(4)],
            activity=[(float(index), "running", str(index)) for index in range(4)],
            completions=[(float(index) + 0.5, str(index)) for index in range(3)],
        )
        with patch(
            "konvu_telemetry.fleet_telemetry._timestamp",
            side_effect=lambda value: (
                float(str(value)[-3:-1]) if isinstance(value, str) else None
            ),
        ):
            _comparable_forecast(session, [telemetry], True)
            self.assertEqual(session["projected_next_10_tasks_usd"], 20.0)
            self.assertEqual(session["forecast_basis"]["sample_count"], 3)
            telemetry.configurations.append((3.0, "other-model", "medium"))
            _comparable_forecast(session, [telemetry], True, (12.5, 7))
        self.assertEqual(session["projected_next_10_tasks_usd"], 12.5)
        self.assertEqual(session["forecast_basis"]["method"], "provider_median_history")
        self.assertEqual(session["forecast_basis"]["sample_count"], 7)
        with patch(
            "konvu_telemetry.fleet_telemetry._timestamp",
            side_effect=lambda value: (
                float(str(value)[-3:-1]) if isinstance(value, str) else None
            ),
        ):
            _comparable_forecast(session, [telemetry], True)
        self.assertEqual(session["projected_next_10_tasks_usd"], 20.0)
        self.assertEqual(session["forecast_basis"]["method"], "sparse_session_prompts")

    def test_baseline_comparison_interpolates_without_unbounded_extrapolation(
        self,
    ) -> None:
        baseline = {
            "providers": {
                "claude": [
                    {
                        "iterations": 5,
                        "sessions": "invalid",
                        "median_cost_usd": 5.0,
                        "median_tokens": 50,
                    },
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
        self.assertIsNone(extrapolated)

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
            "codex",
            10,
            10,
            baseline,
            "model",
            "medium",
            "standard",
            cost_usd=10,
            comparison_scope="model_effort_speed",
        )
        provider = baseline_comparison(
            "codex",
            10,
            10,
            baseline,
            "model",
            "medium",
            "standard",
            cost_usd=10,
            comparison_scope="provider",
        )
        wrong_speed = baseline_comparison(
            "codex",
            10,
            10,
            baseline,
            "model",
            "medium",
            "fast",
            cost_usd=10,
            comparison_scope="model_effort_speed",
        )
        unavailable_match = baseline_comparison(
            "codex",
            10,
            10,
            baseline,
            "model",
            "medium",
            "fast",
            cost_usd=10,
            comparison_scope="model_effort_speed",
        )
        extrapolated_match = baseline_comparison(
            "codex",
            20,
            20,
            baseline,
            "model",
            "medium",
            "standard",
            cost_usd=20,
            comparison_scope="model_effort_speed",
        )
        self.assertEqual(matched["scope"], "model_effort_speed")
        self.assertEqual(matched["speed"], "standard")
        self.assertEqual(provider["scope"], "provider")
        self.assertIsNone(wrong_speed)
        self.assertIsNone(unavailable_match)
        self.assertIsNone(extrapolated_match)
        self.assertEqual(
            baseline["forecasts"]["provider_median_next_10"]["codex"],
            {"median_next_10_usd": 10.0, "sessions": 6},
        )

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
                patch(
                    "konvu_telemetry.snapshot.baseline_comparison",
                    side_effect=lambda *_args, comparison_scope="auto", **_kwargs: {
                        "scope": comparison_scope
                    },
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
        self.assertEqual(session["baseline"]["scope"], "model_effort_speed")
        self.assertEqual(session["baselines"]["provider"]["scope"], "provider")
        self.assertEqual(
            session["baselines"]["model_effort_speed"]["scope"],
            "model_effort_speed",
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
                "baselines": {
                    "provider": baseline_comparison(
                        "claude", count, tokens, baseline, cost_usd=cost
                    )
                },
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
                "baselines": {"provider": {"token_overhead_percent": 150}},
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
            "baseline": {"cost_overhead_percent": 10},
            "baselines": {"provider": {"cost_overhead_percent": 200}},
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "notifications.json"
            with patch(
                "konvu_telemetry.analytics.notification_state_path",
                return_value=state_path,
            ):
                apply_notification_tracking([session], 100.0)
                self.assertEqual(session["notification"]["sequence"], 1)
                self.assertEqual(session["notification"]["baseline_scope"], "provider")
                self.assertEqual(session["notification"]["overhead_percent"], 200)
                session["total_cost_usd"] = 11.0
                session["baselines"] = {"provider": {"cost_overhead_percent": 210}}
                apply_notification_tracking([session], 100.0 + 9 * 60)
                self.assertEqual(session["notification"]["sequence"], 1)
                apply_notification_tracking([session], 100.0 + 10 * 60)
        self.assertEqual(session["notification"]["sequence"], 2)

    def test_hot_alerts_ignore_matched_median_when_provider_median_is_normal(
        self,
    ) -> None:
        session = {
            "id": "session",
            "provider": "claude",
            "total_cost_usd": 10.0,
            "projected_next_10_tasks_usd": 4.01,
            "cost_status": "complete",
            "baseline": {"cost_overhead_percent": 999},
            "baselines": {"provider": {"cost_overhead_percent": 199}},
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "konvu_telemetry.analytics.notification_state_path",
                return_value=Path(directory) / "notifications.json",
            ):
                apply_notification_tracking([session], 100.0)
        self.assertEqual(session["notification"], {"sequence": 0, "hot": False})

    def test_quota_alerts_cross_threshold_then_renotify_only_when_rising(self) -> None:
        sessions = [{"id": "session", "provider": "codex"}]
        account_quotas = {
            "codex": {
                "windows": [
                    {
                        "limit_id": "default",
                        "session_id": "session",
                        "window_minutes": 300,
                        "used_percent": 80.0,
                    },
                    {
                        "limit_id": "default",
                        "session_id": "session",
                        "window_minutes": 10080,
                        "used_percent": 90.0,
                    },
                ]
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "notifications.json"
            with patch(
                "konvu_telemetry.analytics.notification_state_path",
                return_value=state_path,
            ):
                apply_notification_tracking(sessions, 100.0, account_quotas)
                self.assertEqual(
                    account_quotas["codex"]["notifications"],
                    [
                        {
                            "sequence": 1,
                            "hot": True,
                            "window": "5-hour",
                            "used_percent": 80,
                            "session_id": "session",
                        },
                        {
                            "sequence": 1,
                            "hot": True,
                            "window": "weekly",
                            "used_percent": 90,
                            "session_id": "session",
                        },
                    ],
                )
                account_quotas["codex"]["windows"][0]["used_percent"] = 81.0
                apply_notification_tracking(sessions, 100.0 + 9 * 60, account_quotas)
                self.assertEqual(
                    account_quotas["codex"]["notifications"][0]["sequence"], 1
                )
                apply_notification_tracking(sessions, 100.0 + 10 * 60, account_quotas)
                self.assertEqual(
                    account_quotas["codex"]["notifications"][0]["sequence"], 2
                )
                account_quotas["codex"]["windows"][0]["used_percent"] = 20.0
                apply_notification_tracking(sessions, 100.0 + 11 * 60, account_quotas)
                self.assertEqual(
                    account_quotas["codex"]["notifications"],
                    [
                        {
                            "sequence": 1,
                            "hot": True,
                            "window": "weekly",
                            "used_percent": 90,
                            "session_id": "session",
                        }
                    ],
                )
                account_quotas["codex"]["windows"][0]["used_percent"] = 80.0
                apply_notification_tracking(sessions, 100.0 + 12 * 60, account_quotas)
        self.assertEqual(account_quotas["codex"]["notifications"][0]["sequence"], 3)

    def test_quota_usage_text_marks_windows_at_the_alert_threshold(self) -> None:
        self.assertEqual(
            quota_usage_text(
                {
                    "rate_limits": {
                        "five_hour": {"utilization": 0.8},
                        "seven_day": {"utilization": 0.9},
                    }
                }
            ),
            "🔥 ⏳ 80% 5-hour limit · 🔥 📅 90% weekly limit",
        )

    def test_quota_alerts_again_after_a_window_resets_above_threshold(self) -> None:
        sessions = [{"id": "session", "provider": "codex"}]
        account_quotas = {
            "codex": {
                "windows": [
                    {
                        "limit_id": "default",
                        "session_id": "session",
                        "window_minutes": 300,
                        "used_percent": 80.0,
                        "resets_at": "2026-01-01T05:00:00+00:00",
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "notifications.json"
            with patch(
                "konvu_telemetry.analytics.notification_state_path",
                return_value=state_path,
            ):
                apply_notification_tracking(sessions, 100.0, account_quotas)
                account_quotas["codex"]["windows"][0].update(
                    {
                        "used_percent": 80.0,
                        "resets_at": "2026-01-01T10:00:00+00:00",
                    }
                )
                apply_notification_tracking(sessions, 101.0, account_quotas)
        self.assertEqual(account_quotas["codex"]["notifications"][0]["sequence"], 2)

    def test_claude_statusline_quotas_reach_the_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            quota_path = Path(directory) / "claude-quotas.json"
            with patch(
                "konvu_telemetry.display.claude_quota_path", return_value=quota_path
            ):
                record_claude_quotas(
                    {
                        "rate_limits": {
                            "five_hour": {"utilization": 0.8},
                            "seven_day": {"used_percentage": 90},
                        }
                    },
                    "session",
                )
            snapshot = {"sessions": [{"id": "session", "provider": "claude"}]}
            with patch(
                "konvu_telemetry.fleet_telemetry.claude_quota_path",
                return_value=quota_path,
            ):
                enrich_snapshot(snapshot, [], [], time.time())
        quotas = snapshot["account_quotas"]["claude"]
        self.assertEqual(quotas["source"], "claude_statusline")
        self.assertEqual(
            quotas["windows"],
            [
                {
                    "limit_id": "default",
                    "session_id": "session",
                    "window_minutes": 300,
                    "used_percent": 80.0,
                    "remaining_percent": 20.0,
                    "resets_at": None,
                },
                {
                    "limit_id": "default",
                    "session_id": "session",
                    "window_minutes": 10080,
                    "used_percent": 90,
                    "remaining_percent": 10,
                    "resets_at": None,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
