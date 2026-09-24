import json
from collections.abc import Callable
from datetime import datetime, timezone
from io import StringIO
import os
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scripts.update_pricing import validated_payload

from konvu_telemetry import service
from konvu_telemetry.analytics import (
    apply_notification_tracking,
    baseline_comparison,
    build_baselines,
    cumulative_median_checkpoints,
    deduplicate_usage_events,
    forecast_backtest_sample,
    iteration_series,
    load_baselines,
    scaled_precompact_forecast,
    single_configuration,
    single_model_effort,
    task_series,
)
from konvu_telemetry.config import (
    ALERT_FORECAST_USD,
    BASELINE_MILESTONES,
    BASELINE_MIN_SESSIONS,
    BASELINE_SCHEMA_VERSION,
)
from konvu_telemetry.display import (
    baseline_text,
    claude_hook,
    claude_prompt_hook,
    codex_hook,
    codex_is_desktop,
    codex_prompt_hook,
    dashboard_line,
    last_prompt_used_a_tool,
    payload_context_percent,
    quota_usage_text,
    record_claude_quotas,
    refreshed_session,
    statusline,
    usage_box_lines,
    usage_rows,
)
from konvu_telemetry.exporter import normalized_event
from konvu_telemetry.fleet_telemetry import (
    _CACHE as TELEMETRY_CACHE,
    TranscriptTelemetry,
    _comparable_forecast,
    _timestamp,
    _quota_windows,
    enrich_snapshot,
    is_claude_prompt,
    parse_telemetry,
)
from konvu_telemetry.live import CodexLiveFile, IncrementalLiveState
from konvu_telemetry.models import Usage, UsageEvent
from konvu_telemetry.parsers import (
    assistant_event,
    claude_client_in_file,
    claude_hook_transcript,
    codex_events_in_file,
    codex_hook_transcript,
    codex_subagent_parent,
    codex_task_starts,
    events_in_file,
    has_usage_fields,
    is_human_claude_prompt,
    spawned_agent_labels,
    spawned_agent_times,
    transcript_session_id,
    user_prompt_times_by_session,
)
from konvu_telemetry.pricing import (
    claude_context_window,
    cost_status,
    event_cost,
    load_pricing,
)
from konvu_telemetry.service import (
    DashboardRequestHandler,
    RefreshCoordinator,
    collect_forever,
    load_health,
    local_request_allowed,
    snapshot_has_dashboard_data,
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


DASHBOARD_HINT = "🔗 run konvu-telemetry setup to start the dashboard"


def health_patch(health: object) -> object:
    """Patch the display's health read with a value, a real reader, or a failure."""
    if isinstance(health, BaseException) or callable(health):
        return patch("konvu_telemetry.display.load_health", side_effect=health)
    return patch("konvu_telemetry.display.load_health", return_value=health)


class ServiceTests(unittest.TestCase):
    def test_usage_completeness_rejects_boolean_token_counters(self) -> None:
        self.assertTrue(
            has_usage_fields(
                {"input_tokens": 1, "output_tokens": 0}, "input_tokens", "output_tokens"
            )
        )
        self.assertFalse(
            has_usage_fields(
                {"input_tokens": True, "output_tokens": 0},
                "input_tokens",
                "output_tokens",
            )
        )

    def test_hook_transcripts_must_match_the_claimed_session(self) -> None:
        claimed = "11111111-1111-1111-1111-111111111111"
        other = "22222222-2222-2222-2222-222222222222"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = root / "claude.jsonl"
            claude.write_text(json.dumps({"sessionId": other}) + "\n")
            codex = root / f"rollout-{other}.jsonl"
            codex.write_text("{}\n")
            with patch.dict(
                os.environ,
                {
                    "KONVU_LIVE_USAGE_CLAUDE_DIR": directory,
                    "KONVU_LIVE_USAGE_CODEX_DIR": directory,
                },
            ):
                self.assertIsNone(
                    claude_hook_transcript({"transcript_path": str(claude)}, claimed)
                )
                self.assertIsNone(
                    codex_hook_transcript({"transcript_path": str(codex)}, claimed)
                )
                self.assertEqual(
                    claude_hook_transcript({"transcript_path": str(claude)}, other),
                    claude,
                )
                self.assertEqual(
                    codex_hook_transcript({"transcript_path": str(codex)}, other),
                    codex,
                )

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
                        "median_method": "checkpoint_cohort_medians",
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

    def test_claude_context_window_uses_the_full_published_input_capacity(self) -> None:
        prices = {
            "claude-test": {
                "context_window_tokens": 1_000_000.0,
            },
            "claude-small": {
                "context_window_tokens": 16_384.0,
            },
        }

        self.assertEqual(claude_context_window("claude-test", prices), 1_000_000)
        self.assertEqual(claude_context_window("claude-small", prices), 16_384)
        self.assertIsNone(claude_context_window("unknown", prices))
        self.assertIsNone(
            claude_context_window(
                "invalid", {"invalid": {"context_window_tokens": float("inf")}}
            )
        )

    def test_baseline_milestones_cover_long_sessions(self) -> None:
        self.assertEqual(
            BASELINE_MILESTONES,
            (
                *range(1, 101),
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
            total: int,
            input_tokens: int,
            output_tokens: int,
            reasoning_output_tokens: int,
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
                            "reasoning_output_tokens": reasoning_output_tokens,
                        },
                        "last_token_usage": {
                            "input_tokens": input_tokens,
                            "cached_input_tokens": 0,
                            "cache_write_input_tokens": 0,
                            "output_tokens": output_tokens,
                            "reasoning_output_tokens": reasoning_output_tokens,
                        },
                        "model_context_window": 258_400,
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
                    for record in [checkpoint(15, 10, 2, 3), checkpoint(34, 12, 3, 4)]
                )
            )
            events = list(codex_events_in_file(transcript))
        self.assertEqual([event.usage.output_tokens for event in events], [2, 3])
        self.assertEqual(
            [event.usage.reasoning_output_tokens for event in events], [3, 4]
        )
        self.assertEqual([event.usage.total_tokens for event in events], [15, 19])
        self.assertEqual(
            [event.context_window_tokens for event in events], [258_400, 258_400]
        )

    def test_codex_user_messages_are_task_boundaries_without_task_started(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "rollout-session.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        {
                            "timestamp": "2026-01-01T00:00:00Z",
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "one"},
                        },
                        {
                            "timestamp": "2026-01-01T00:01:00Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": "two",
                            },
                        },
                    )
                )
            )
            starts = codex_task_starts(transcript)
        self.assertEqual(starts, [1767225600.0, 1767225660.0])

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

    def test_codex_reasoning_output_is_priced_and_exported(self) -> None:
        event = UsageEvent(
            provider="codex",
            session_id="session",
            message_id="message",
            timestamp=0,
            model="gpt-test",
            usage=Usage(0, 2, 0, 0, 0, 0, "standard", 3),
            tool_calls=0,
            is_subagent=False,
            agent_id=None,
            effort="high",
        )
        prices = {
            "gpt-test": {
                "input": 1,
                "output": 2,
                "cache_write": 1,
                "cache_read": 1,
                "web_search": 0,
                "fast_multiplier": 1,
            }
        }

        self.assertEqual(event_cost(event, prices), 10.0)
        exported = normalized_event(event, prices)
        self.assertEqual(exported["tokens"]["reasoning_output"], 3)
        self.assertEqual(exported["reasoning_effort"], "high")
        self.assertEqual(exported["speed"], "standard")
        self.assertTrue(exported["usage_complete"])
        self.assertIsNone(exported["estimated_credit_equivalent"])

    def test_missing_billable_usage_is_not_reported_as_a_complete_cost(self) -> None:
        event = assistant_event(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "session",
                "message": {
                    "id": "message",
                    "role": "assistant",
                    "model": "claude-test",
                    "usage": {"input_tokens": 10},
                },
            }
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertFalse(event.usage.complete)
        self.assertEqual(
            cost_status(
                [event],
                {
                    "claude-test": {
                        "input": 1,
                        "output": 1,
                        "cache_write": 1,
                        "cache_read": 1,
                        "fast_multiplier": 1,
                    }
                },
            ),
            ("unavailable", 1),
        )

    def test_codex_checkpoint_identity_deduplicates_replayed_rollouts(self) -> None:
        first = UsageEvent(
            "codex",
            "session",
            "session:100",
            1,
            "model",
            Usage(1, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "standard",
        )
        replay = UsageEvent(
            "codex",
            "session",
            "session:100",
            2,
            "model",
            Usage(1, 0, 0, 0, 0, 0, "standard"),
            0,
            False,
            None,
            "standard",
        )
        self.assertEqual(deduplicate_usage_events([first, replay]), [first])

    def test_ratio_quota_values_are_normalized_to_percent(self) -> None:
        windows = _quota_windows(
            {"primary": {"used_percent": 0.8, "window_minutes": 300}}, 0
        )
        self.assertEqual(windows[0]["used_percent"], 80.0)

    def test_claude_sdk_client_is_explicitly_attributed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "sessionId": "session",
                        "entrypoint": "sdk-cli",
                    }
                )
            )
            clients = claude_client_in_file(transcript)
        self.assertEqual(clients, {"session": "sdk"})

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

    def test_cumulative_median_uses_sessions_that_reached_each_checkpoint(self) -> None:
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
        self.assertEqual(points[9]["median_cost_usd"], 35.0)
        self.assertEqual(points[19]["median_cost_usd"], 65.0)

    def test_cumulative_median_does_not_carry_finished_sessions_forward(self) -> None:
        series = [
            *[[(100.0, 100)] * 10 for _ in range(5)],
            [(1.0, 1)] * 20,
            [(2.0, 2)] * 20,
            [(3.0, 3)] * 20,
            [(4.0, 4)] * 20,
            [(5.0, 5)] * 20,
        ]
        points = cumulative_median_checkpoints(series)
        self.assertEqual(points[9]["median_cost_usd"], 525.0)
        self.assertEqual(points[19]["median_cost_usd"], 555.0)
        self.assertEqual(points[9]["median_tokens"], 525)
        self.assertEqual(points[19]["median_tokens"], 555)

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

    def test_fleet_telemetry_keeps_oversized_codex_compaction(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "source": "cli",
                    "base_instructions": "x" * 1_000_000,
                },
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "type": "compacted",
                "payload": {"replacement_history": ["x" * 1_000_000]},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{session_id}.jsonl"
            transcript.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n"
            )
            TELEMETRY_CACHE.clear()
            parsed = parse_telemetry(transcript, "codex")
        self.assertEqual(parsed.session_id, session_id)
        self.assertEqual(
            parsed.compactions,
            [
                {
                    "timestamp": "2026-01-01T00:00:01+00:00",
                    "source": "codex_compacted",
                }
            ],
        )

    def test_incremental_reader_keeps_oversized_codex_guardian_parent(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        parent_id = "00000000-0000-0000-0000-000000000002"
        session_meta = {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "parent_thread_id": parent_id,
                "originator": "codex-tui",
                "source": {"subagent": {"other": "guardian"}},
                "base_instructions": "x" * 1_000_000,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{session_id}.jsonl"
            transcript.write_text(json.dumps(session_meta) + "\n")
            state = IncrementalLiveState()._refresh_codex(transcript)
        self.assertEqual(state.parent, (parent_id, "guardian"))
        self.assertEqual(state.client, "cli")

    def test_incremental_reader_keeps_oversized_codex_spawn_metadata(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        parent_id = "00000000-0000-0000-0000-000000000002"
        session_meta = {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "parent_thread_id": parent_id,
                "originator": "Codex Desktop",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "agent_path": "/root/reviewer",
                            "agent_nickname": "Hubble",
                        }
                    }
                },
                "base_instructions": "x" * 1_000_000,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{session_id}.jsonl"
            transcript.write_text(json.dumps(session_meta) + "\n")
            state = IncrementalLiveState()._refresh_codex(transcript)
        self.assertEqual(state.parent, (parent_id, "Hubble"))
        self.assertEqual(state.client, "desktop")

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
            "median_method": "checkpoint_cohort_medians",
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
            "median_method": "checkpoint_cohort_medians",
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

    def test_dashboard_open_records_whether_visible_data_exists(self) -> None:
        handler = object.__new__(DashboardRequestHandler)
        handler.headers = {"Host": "127.0.0.1:7824"}
        handler.path = "/"
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "live-sessions.json"
            snapshot.write_text('{"generated_at":"2026-01-01T00:00:00Z","sessions":[]}')
            with (
                patch("konvu_telemetry.service.snapshot_path", return_value=snapshot),
                patch("konvu_telemetry.service.record_dashboard_opened") as recorded,
                patch("http.server.SimpleHTTPRequestHandler.do_GET"),
            ):
                DashboardRequestHandler.do_GET(handler)
        recorded.assert_called_once_with(data_available=False)

    def test_dashboard_open_does_not_read_snapshot_for_analytics(self) -> None:
        handler = object.__new__(DashboardRequestHandler)
        handler.headers = {"Host": "127.0.0.1:7824"}
        handler.path = "/"
        with (
            patch.object(service, "_DASHBOARD_DATA_AVAILABLE", True, create=True),
            patch(
                "konvu_telemetry.service.snapshot_path",
                side_effect=AssertionError("request read snapshot"),
            ),
            patch("konvu_telemetry.service.record_dashboard_opened") as recorded,
            patch("http.server.SimpleHTTPRequestHandler.do_GET"),
        ):
            DashboardRequestHandler.do_GET(handler)

        recorded.assert_called_once_with(data_available=True)

    def test_dashboard_data_matches_the_visible_activity_window(self) -> None:
        snapshot = {
            "live_activity_window_seconds": 1_200,
            "sessions": [
                {"last_activity_at": "2026-01-01T00:00:00+00:00"},
            ],
        }

        self.assertTrue(snapshot_has_dashboard_data(snapshot, 1_767_225_630.0))
        self.assertFalse(snapshot_has_dashboard_data(snapshot, 1_767_226_801.0))

    def test_service_initializes_dashboard_visibility_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "live-sessions.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "live_activity_window_seconds": 1_200,
                        "sessions": [{"last_activity_at": "2026-01-01T00:00:00+00:00"}],
                    }
                )
            )
            service._DASHBOARD_DATA_AVAILABLE = False
            with patch("konvu_telemetry.service.snapshot_path", return_value=snapshot):
                service.initialize_dashboard_data_available(1_767_225_630.0)

        self.assertTrue(service._DASHBOARD_DATA_AVAILABLE)

    def test_refreshed_session_reads_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session.json"
            session.write_text('{"id":"session"}')
            with patch("konvu_telemetry.display.session_path", return_value=session):
                payload = refreshed_session(
                    "claude", "00000000-0000-0000-0000-000000000001"
                )
        self.assertEqual(payload, {"id": "session"})

    def test_refreshed_session_reads_stale_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session.json"
            session.write_text('{"id":"fallback"}')
            stale_at = time.time() - 24 * 60 * 60
            os.utime(session, (stale_at, stale_at))
            with patch("konvu_telemetry.display.session_path", return_value=session):
                payload = refreshed_session(
                    "claude", "00000000-0000-0000-0000-000000000001"
                )
        self.assertEqual(payload, {"id": "fallback"})

    def run_claude_hook(
        self,
        hook: Callable[[], None],
        entrypoint: str | None,
        session: dict[str, object] | None,
        health: object = None,
    ) -> str:
        """Run a Claude hook against one client entrypoint and return its stdout."""
        session_id = "00000000-0000-0000-0000-000000000001"
        stdout = StringIO()
        environment = (
            {} if entrypoint is None else {"CLAUDE_CODE_ENTRYPOINT": entrypoint}
        )
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                sys, "stdin", StringIO(json.dumps({"session_id": session_id}))
            ),
            patch.object(sys, "stdout", stdout),
            patch("konvu_telemetry.display.refreshed_session", return_value=session),
            patch("konvu_telemetry.display.recorded_quota_usage_text", return_value=""),
            health_patch({"status": "starting"} if health is None else health),
        ):
            hook()
        return stdout.getvalue()

    def test_claude_stop_hook_prints_nothing_for_any_client(self) -> None:
        session = {
            "id": "00000000-0000-0000-0000-000000000001",
            "total_cost_usd": 25.0,
            "cost_status": "complete",
            "usage_mode": "exhausted",
            "projected_next_10_tasks_usd": 5.0,
            "task_count": 9,
            "context_tokens": 500,
            "context_window_tokens": 1000,
        }
        for entrypoint in ("cli", "claude-desktop", None):
            self.assertEqual(self.run_claude_hook(claude_hook, entrypoint, session), "")

    def test_claude_prompt_hook_injects_context_only_in_the_desktop_app(self) -> None:
        session = {
            "id": "00000000-0000-0000-0000-000000000001",
            "total_cost_usd": 25.0,
            "cost_status": "complete",
            "usage_mode": "exhausted",
            "projected_next_10_tasks_usd": 5.0,
            "last_task_tool_calls": 3,
            "context_tokens": 500,
            "context_window_tokens": 1000,
        }
        payload = json.loads(
            self.run_claude_hook(claude_prompt_hook, "claude-desktop", session)
        )
        self.assertNotIn("systemMessage", payload)
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit"
        )
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("verbatim as the very last thing in your reply", context)
        self.assertNotIn("```", context)
        self.assertIn("╭─ Konvu usage", context)
        self.assertIn(
            "│ 💸 $25.0 API-equivalent total · $5.0 API-equivalent for the next 10 prompts",
            context,
        )
        self.assertTrue(context.endswith("╰─"))
        self.assertEqual(self.run_claude_hook(claude_prompt_hook, "cli", session), "")
        self.assertEqual(self.run_claude_hook(claude_prompt_hook, None, session), "")

    def test_claude_prompt_hook_stays_silent_when_the_session_file_is_missing(
        self,
    ) -> None:
        self.assertEqual(
            self.run_claude_hook(claude_prompt_hook, "claude-desktop", None), ""
        )

    def run_codex_hook(
        self,
        hook: Callable[[], None],
        client: str,
        session: dict[str, object] | None,
        turn_tool_calls: int = 1,
        health: object = None,
    ) -> str:
        """Run a Codex hook against one recorded client and return its stdout."""
        session_id = "00000000-0000-0000-0000-000000000001"
        stdout = StringIO()
        with (
            patch.object(
                sys,
                "stdin",
                StringIO(json.dumps({"session_id": session_id, "turn_id": "turn"})),
            ),
            patch.object(sys, "stdout", stdout),
            patch(
                "konvu_telemetry.display.codex_hook_transcript",
                return_value=Path("session.jsonl"),
            ),
            patch(
                "konvu_telemetry.display.codex_turn_tool_calls",
                return_value=turn_tool_calls,
            ),
            patch("konvu_telemetry.display.codex_client_in_file", return_value=client),
            patch("konvu_telemetry.display.refreshed_session", return_value=session),
            patch(
                "konvu_telemetry.display.recorded_quota_usage_text",
                return_value="3% weekly limit",
            ),
            health_patch({"status": "starting"} if health is None else health),
        ):
            hook()
        return stdout.getvalue()

    def test_codex_stop_hook_keeps_cli_output_and_suppresses_the_desktop_app(
        self,
    ) -> None:
        session = {
            "id": "00000000-0000-0000-0000-000000000001",
            "total_cost_usd": 25.4,
            "task_count": 5,
            "cost_status": "complete",
            "usage_mode": "exhausted",
            "projected_next_10_tasks_usd": 4.9,
            "context_tokens": 650,
            "context_window_tokens": 1000,
        }
        # Byte-for-byte: quota rides the context line rather than appearing on one of its
        # own, and the dashboard row is the last line inside the frame.
        self.assertEqual(
            json.loads(self.run_codex_hook(codex_hook, "cli", session)),
            {
                "systemMessage": "\n╭─ Konvu usage\n"
                "│ 💸 $25.4 API-equivalent total · $4.9 API-equivalent for the next 10 prompts\n"
                "│ 🧠 65% context · 3% weekly limit\n"
                "│ 🔗 run konvu-telemetry setup to start the dashboard\n"
                "╰─"
            },
        )
        self.assertEqual(
            json.loads(self.run_codex_hook(codex_hook, "unknown", session)).keys(),
            {"systemMessage"},
        )
        self.assertEqual(
            json.loads(self.run_codex_hook(codex_hook, "desktop", session)),
            {"suppressOutput": True},
        )

    def test_codex_prompt_hook_injects_context_only_for_the_desktop_originator(
        self,
    ) -> None:
        session = {
            "id": "00000000-0000-0000-0000-000000000001",
            "total_cost_usd": 25.4,
            "task_count": 5,
            "cost_status": "complete",
            "projected_next_10_tasks_usd": 4.9,
            "last_task_tool_calls": 3,
            "context_tokens": 650,
            "context_window_tokens": 1000,
        }
        payload = json.loads(self.run_codex_hook(codex_prompt_hook, "desktop", session))
        self.assertNotIn("systemMessage", payload)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("verbatim as the very last thing in your reply", context)
        self.assertIn("│ 🧠 65% context · 3% weekly limit", context)
        for client in ("cli", "unknown"):
            self.assertEqual(
                json.loads(self.run_codex_hook(codex_prompt_hook, client, session)),
                {"suppressOutput": True},
            )
        self.assertEqual(
            json.loads(self.run_codex_hook(codex_prompt_hook, "desktop", None)),
            {"suppressOutput": True},
        )

    def test_last_prompt_used_a_tool_accepts_only_a_positive_count(self) -> None:
        self.assertTrue(last_prompt_used_a_tool({"last_task_tool_calls": 1}))
        self.assertTrue(last_prompt_used_a_tool({"last_task_tool_calls": 12}))
        for value in (0, -1, None, "1", 1.0, True, [1]):
            self.assertFalse(
                last_prompt_used_a_tool({"last_task_tool_calls": value}), repr(value)
            )
        self.assertFalse(last_prompt_used_a_tool({}))

    def usage_session(self, tool_calls: object) -> dict[str, object]:
        """Build a session that differs only in the field the display gate reads."""
        session: dict[str, object] = {
            "id": "00000000-0000-0000-0000-000000000001",
            "total_cost_usd": 0.02,
            "cost_status": "complete",
            "usage_mode": "exhausted",
            "projected_next_10_tasks_usd": 0.1,
            "task_count": 1,
            "context_tokens": 500,
            "context_window_tokens": 1000,
        }
        if tool_calls is not None:
            session["last_task_tool_calls"] = tool_calls
        return session

    def injected_context(self, stdout: str) -> str:
        """Read the context a prompt hook asked the client to append."""
        payload = json.loads(stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIsInstance(context, str)
        return str(context)

    def test_desktop_boxes_show_only_when_the_last_prompt_used_a_tool(self) -> None:
        self.assertIn(
            "💸 ",
            self.injected_context(
                self.run_claude_hook(
                    claude_prompt_hook, "claude-desktop", self.usage_session(1)
                )
            ),
        )
        self.assertIn(
            "💸 ",
            self.injected_context(
                self.run_codex_hook(codex_prompt_hook, "desktop", self.usage_session(1))
            ),
        )
        # Zero, absent, and non-integer counts all fail closed on both desktop surfaces.
        for tool_calls in (0, None, "1", 1.5):
            session = self.usage_session(tool_calls)
            self.assertEqual(
                self.run_claude_hook(claude_prompt_hook, "claude-desktop", session),
                "",
                repr(tool_calls),
            )
            self.assertEqual(
                json.loads(self.run_codex_hook(codex_prompt_hook, "desktop", session)),
                {"suppressOutput": True},
                repr(tool_calls),
            )

    def test_codex_stop_box_shows_only_when_the_turn_used_a_tool(self) -> None:
        # This hook counts the exact turn it fires on rather than the snapshot field.
        session = self.usage_session(0)
        self.assertIn(
            "╭─ Konvu usage",
            json.loads(
                self.run_codex_hook(codex_hook, "cli", session, turn_tool_calls=1)
            )["systemMessage"],
        )
        self.assertEqual(
            json.loads(
                self.run_codex_hook(codex_hook, "cli", session, turn_tool_calls=0)
            ),
            {"suppressOutput": True},
        )

    def run_statusline(
        self,
        session: dict[str, object] | None,
        health: object,
        payload_extra: dict[str, object] | None = None,
    ) -> str:
        """Render the Claude CLI status line against one session and health state."""
        session_id = "00000000-0000-0000-0000-000000000001"
        payload: dict[str, object] = {"session_id": session_id, **(payload_extra or {})}
        stdout = StringIO()
        with (
            patch.object(sys, "stdin", StringIO(json.dumps(payload))),
            patch.object(sys, "stdout", stdout),
            patch(
                "konvu_telemetry.display.claude_hook_transcript",
                return_value=Path("session.jsonl"),
            ),
            patch("konvu_telemetry.display.refreshed_session", return_value=session),
            health_patch(health),
        ):
            statusline()
        return stdout.getvalue()

    def surface_outputs(self, health: object) -> dict[str, str]:
        """Render the text all four usage surfaces show for one collector health state."""
        session = self.usage_session(1)
        return {
            "claude_desktop": self.injected_context(
                self.run_claude_hook(
                    claude_prompt_hook, "claude-desktop", session, health
                )
            ),
            "codex_desktop": self.injected_context(
                self.run_codex_hook(
                    codex_prompt_hook, "desktop", session, health=health
                )
            ),
            "codex_cli": str(
                json.loads(
                    self.run_codex_hook(codex_hook, "cli", session, health=health)
                )["systemMessage"]
            ),
            "statusline": self.run_statusline(session, health),
        }

    def test_every_usage_surface_links_the_dashboard_when_the_collector_is_healthy(
        self,
    ) -> None:
        # The port comes from configuration, so an overridden one reaches every surface.
        with patch("konvu_telemetry.display.DASHBOARD_PORT", 9999):
            outputs = self.surface_outputs({"status": "healthy"})
        link = "🔗 dashboard: http://127.0.0.1:9999/"
        for surface, output in outputs.items():
            self.assertIn(link, output, surface)
            self.assertNotIn("konvu-telemetry setup", output, surface)
        for surface in ("claude_desktop", "codex_desktop", "codex_cli"):
            self.assertIn(f"│ {link}\n╰─", outputs[surface], surface)
        self.assertTrue(outputs["statusline"].endswith(f"{link}\n"))

    def test_every_usage_surface_points_at_setup_when_the_collector_is_not_healthy(
        self,
    ) -> None:
        # Stale, starting, error, and any unrecognized status all fail to the hint.
        for status in ("stale", "starting", "error", "", "HEALTHY"):
            outputs = self.surface_outputs({"status": status})
            for surface, output in outputs.items():
                self.assertIn(DASHBOARD_HINT, output, (status, surface))
                self.assertNotIn("http://127.0.0.1", output, (status, surface))
            for surface in ("claude_desktop", "codex_desktop", "codex_cli"):
                self.assertIn(
                    f"│ {DASHBOARD_HINT}\n╰─", outputs[surface], (status, surface)
                )
            self.assertTrue(
                outputs["statusline"].endswith(f"{DASHBOARD_HINT}\n"), status
            )

    def test_every_usage_surface_points_at_setup_when_health_cannot_be_read(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "health.json"
            unreadable = Path(directory) / "corrupt.json"
            unreadable.write_text("{not json")
            for path in (missing, unreadable):
                with patch("konvu_telemetry.service.health_path", return_value=path):
                    self.assertEqual(dashboard_line(), DASHBOARD_HINT, path.name)
                    for surface, output in self.surface_outputs(
                        service.load_health
                    ).items():
                        self.assertIn(DASHBOARD_HINT, output, (path.name, surface))

    def test_every_usage_surface_points_at_setup_when_reading_health_raises(
        self,
    ) -> None:
        boom = RuntimeError("health exploded")
        for surface, output in self.surface_outputs(boom).items():
            self.assertIn(DASHBOARD_HINT, output, surface)
            self.assertNotIn("http://127.0.0.1", output, surface)

    def shared_row_session(self) -> dict[str, object]:
        """Build a session that exercises every optional row of the shared summary."""
        return {
            "id": "00000000-0000-0000-0000-000000000001",
            "total_cost_usd": 25.4,
            "cost_status": "complete",
            "usage_mode": "exhausted",
            "projected_next_10_tasks_usd": 4.9,
            "last_task_tool_calls": 3,
            "context_tokens": 500,
            "context_window_tokens": 1000,
            "subagent_total": 2,
            "active_subagents": 1,
            "subagent_entry_context_tokens": 900,
            "subagent_cost_usd": 0.4,
            "baseline": {
                "emoji": "🟢",
                "cost_overhead_percent": -12,
                "iterations": 4,
            },
        }

    def test_usage_rows_are_the_content_every_surface_renders(self) -> None:
        with health_patch({"status": "stale"}):
            rows = usage_rows(self.shared_row_session(), "3% weekly limit")
        self.assertEqual(
            rows,
            [
                "💸 $25.4 API-equivalent total · $4.9 API-equivalent for the next 10 prompts",
                "🤖 1 live / 2 total · 90% context shared · $0.4 API-equivalent",
                "🧠 50% context · 3% weekly limit",
                DASHBOARD_HINT,
            ],
        )

    def test_included_usage_rows_hide_monetary_estimates(self) -> None:
        session = {
            **self.shared_row_session(),
            "usage_mode": "included",
            "quota_attribution": {
                "windows": [{"period": "five_hour", "estimated_percent": 1.25}]
            },
        }
        with health_patch({"status": "stale"}):
            rows = usage_rows(session, "20% 5-hour limit")
        self.assertEqual(
            rows[:3],
            [
                "🟢 Included",
                "20% 5-hour limit",
                "🧠 50% context · Responsible for ~1.2% of 5-hour limit",
            ],
        )
        self.assertNotIn("$", "\n".join(rows))

    def test_unknown_subscription_usage_hides_monetary_estimates(self) -> None:
        session = {**self.shared_row_session(), "usage_mode": "unknown"}
        with health_patch({"status": "stale"}):
            rows = usage_rows(session, "")
        self.assertEqual(rows[0], "⚪ Subscription limit unavailable")
        self.assertNotIn("$", "\n".join(rows))

    def test_the_usage_box_is_the_shared_rows_inside_a_frame(self) -> None:
        session = self.shared_row_session()
        with health_patch({"status": "stale"}):
            lines = usage_box_lines(session, "3% weekly limit")
            rows = usage_rows(session, "3% weekly limit")
        self.assertEqual(lines[0], "╭─ Konvu usage")
        self.assertEqual(lines[-1], "╰─")
        self.assertEqual(lines[1:-1], [f"│ {row}" for row in rows])

    def test_the_status_line_prints_the_shared_rows_unframed(self) -> None:
        # Mutation guard: a status line that renders its own rows again fails here.
        session = self.shared_row_session()
        quotas = {"five_hour": {"utilization": 0.03}}
        output = self.run_statusline(
            session,
            {"status": "stale"},
            {"rate_limits": quotas, "context_window": {"used_percentage": 87.4}},
        )
        with health_patch({"status": "stale"}):
            rows = usage_rows(session, quota_usage_text({"rate_limits": quotas}), 87.4)
        self.assertEqual(output, "".join(f"{row}\n" for row in rows))
        self.assertIn("🧠 87% context · ⏳ 3% 5-hour limit\n", output)
        self.assertNotIn("│", output)
        self.assertNotIn("╭", output)

    def test_the_status_line_prefers_the_payload_context_percentage(self) -> None:
        # Claude reports the live window to the status line; the snapshot lags a turn.
        session = self.shared_row_session()
        output = self.run_statusline(
            session, {"status": "stale"}, {"context_window": {"used_percentage": 87.4}}
        )
        self.assertIn("🧠 87% context\n", output)
        with health_patch({"status": "stale"}):
            self.assertIn("│ 🧠 50% context", usage_box_lines(session, ""))

    def test_an_unusable_payload_context_falls_back_to_the_snapshot(self) -> None:
        for context_window in (None, {}, {"used_percentage": True}, "50%"):
            self.assertIsNone(
                payload_context_percent({"context_window": context_window}),
                repr(context_window),
            )
            output = self.run_statusline(
                self.shared_row_session(),
                {"status": "stale"},
                {"context_window": context_window},
            )
            self.assertIn("🧠 50% context\n", output, repr(context_window))
        self.assertIsNone(payload_context_percent({}))
        for value in (float("nan"), float("inf")):
            self.assertIsNone(
                payload_context_percent({"context_window": {"used_percentage": value}}),
                repr(value),
            )
        self.assertEqual(
            payload_context_percent({"context_window": {"used_percentage": 0}}), 0.0
        )

    def run_hook_command(
        self, command: str, stdin: str, home: Path
    ) -> "subprocess.CompletedProcess[str]":
        """Invoke one subcommand the way an installed hook does, in its own process."""
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from konvu_telemetry.collector import main; main()",
                command,
            ],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "KONVU_LIVE_USAGE_HOME": str(home),
                "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
            },
        )

    def test_hooks_never_block_a_prompt(self) -> None:
        commands = (
            "claude-hook",
            "claude-prompt-hook",
            "codex-hook",
            "codex-prompt-hook",
            # A build that predates a subcommand must still exit 0 rather than exit 2.
            "some-future-hook",
        )
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "no-telemetry-here"
            for command in commands:
                for stdin in ("", "not json at all", "[]", '{"session_id":"../x"}'):
                    result = self.run_hook_command(command, stdin, empty)
                    self.assertEqual(result.returncode, 0, (command, stdin))
                    self.assertEqual(result.stderr, "", (command, stdin))
                    self.assertNotIn("Konvu live usage", result.stdout, command)

    def test_hooks_stay_silent_when_rendering_raises(self) -> None:
        session = self.usage_session(1)
        boom = RuntimeError("render exploded")
        with patch(
            "konvu_telemetry.display.usage_box_lines", side_effect=boom
        ) as render:
            self.assertEqual(
                self.run_claude_hook(claude_prompt_hook, "claude-desktop", session), ""
            )
            self.assertEqual(
                self.run_codex_hook(codex_prompt_hook, "desktop", session), ""
            )
            self.assertEqual(self.run_codex_hook(codex_hook, "cli", session), "")
        self.assertEqual(render.call_count, 3)
        # The helper patches refreshed_session itself, so raise from the gate instead.
        with patch(
            "konvu_telemetry.display.last_prompt_used_a_tool", side_effect=boom
        ) as lookup:
            self.assertEqual(
                self.run_claude_hook(claude_prompt_hook, "claude-desktop", session), ""
            )
            self.assertEqual(
                self.run_codex_hook(codex_prompt_hook, "desktop", session), ""
            )
        self.assertEqual(lookup.call_count, 2)

    def test_codex_is_desktop_covers_every_recorded_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            meta = {"type": "session_meta", "payload": {"originator": "Codex Desktop"}}
            vscode = root / "vscode.jsonl"
            vscode.write_text(
                json.dumps({"type": "session_meta", "payload": {"source": "vscode"}})
                + "\n"
            )
            desktop = root / "desktop.jsonl"
            desktop.write_text(json.dumps(meta) + "\n")
            late = root / "late.jsonl"
            late.write_text(
                json.dumps({"type": "event_msg", "payload": {}})
                + "\n"
                + json.dumps(meta)
                + "\n"
            )
            tui = root / "tui.jsonl"
            tui.write_text(
                json.dumps(
                    {"type": "session_meta", "payload": {"originator": "codex-tui"}}
                )
                + "\n"
            )
            exec_run = root / "exec.jsonl"
            exec_run.write_text(
                json.dumps(
                    {"type": "session_meta", "payload": {"originator": "codex_exec"}}
                )
                + "\n"
            )
            broken = root / "broken.jsonl"
            broken.write_text("not json\n")
            for path in (vscode, desktop, late):
                self.assertTrue(codex_is_desktop(path), path.name)
            for path in (tui, exec_run, broken, root / "missing.jsonl"):
                self.assertFalse(codex_is_desktop(path), path.name)
            self.assertFalse(codex_is_desktop(None))

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
        self.assertEqual(health["next_poll_at"], "1970-01-01T00:02:40+00:00")

    def test_collector_keeps_polling_when_health_state_cannot_be_written(self) -> None:
        coordinator = Mock()
        coordinator.wait_for_refresh.side_effect = StopIteration
        with (
            patch(
                "konvu_telemetry.service.build_snapshot", side_effect=OSError("full")
            ),
            patch("konvu_telemetry.service.write_health", side_effect=OSError("full")),
            patch("konvu_telemetry.service.record_collector_failure") as recorded,
            self.assertLogs("konvu_telemetry.service", level="ERROR"),
            self.assertRaises(StopIteration),
        ):
            collect_forever(
                60,
                IncrementalLiveState(),
                Lock(),
                coordinator,
                Mock(refresh=Mock(return_value={})),
            )
        recorded.assert_called_once()
        coordinator.start_collection.assert_called_once_with()
        coordinator.finish_collection.assert_called_once_with("OSError: full")

    def test_manual_refresh_wakes_collector_and_waits_for_completion(self) -> None:
        coordinator = RefreshCoordinator()
        results: list[str | None] = []
        requester = Thread(
            target=lambda: results.append(coordinator.request_refresh(1))
        )

        requester.start()
        self.assertTrue(coordinator.wait_for_refresh(1))
        coordinator.start_collection()
        coordinator.finish_collection(None)
        requester.join(1)

        self.assertFalse(requester.is_alive())
        self.assertEqual(results, [None])

    def test_refresh_during_collection_reuses_in_flight_result(self) -> None:
        coordinator = RefreshCoordinator()
        coordinator.start_collection()
        results: list[str | None] = []
        waiting = Event()
        original_wait_for = coordinator._condition.wait_for

        def tracked_wait_for(
            predicate: Callable[[], bool], timeout: float | None = None
        ) -> bool:
            waiting.set()
            return original_wait_for(predicate, timeout)

        requester = Thread(
            target=lambda: results.append(coordinator.request_refresh(1))
        )

        with patch.object(
            coordinator._condition, "wait_for", side_effect=tracked_wait_for
        ):
            requester.start()
            self.assertTrue(waiting.wait(1))
            coordinator.finish_collection(None)
        requester.join(1)

        self.assertFalse(requester.is_alive())
        self.assertEqual(results, [None])
        self.assertFalse(coordinator.wait_for_refresh(0))

    def test_dashboard_refresh_endpoint_requests_collection(self) -> None:
        handler = object.__new__(DashboardRequestHandler)
        handler.headers = {"Host": "127.0.0.1:7824", "Origin": "http://localhost:7824"}
        handler.path = "/api/refresh"
        handler.refresh_coordinator = Mock()
        handler.refresh_coordinator.request_refresh.return_value = None
        handler.send_response = Mock()
        handler._secure_headers = Mock()
        handler._write_payload = Mock()

        with patch(
            "konvu_telemetry.service.load_health", return_value={"status": "healthy"}
        ):
            DashboardRequestHandler.do_POST(handler)

        handler.refresh_coordinator.request_refresh.assert_called_once_with()
        handler.send_response.assert_called_once_with(200)
        handler._write_payload.assert_called_once_with(b'{"status": "healthy"}')

    def test_first_snapshot_with_session_data_is_recorded(self) -> None:
        service._DASHBOARD_DATA_AVAILABLE = False
        coordinator = Mock()
        coordinator.wait_for_refresh.side_effect = StopIteration
        with (
            patch(
                "konvu_telemetry.service.build_snapshot",
                return_value={
                    "live_activity_window_seconds": 1_200,
                    "sessions": [{"last_activity_at": "2026-01-01T00:00:00+00:00"}],
                },
            ),
            patch("konvu_telemetry.service.write_snapshot"),
            patch("konvu_telemetry.service.write_health"),
            patch("konvu_telemetry.service.record_first_snapshot_ready") as recorded,
            patch("konvu_telemetry.service.time.time", return_value=1_767_225_630.0),
            self.assertRaises(StopIteration),
        ):
            collect_forever(
                60,
                IncrementalLiveState(),
                Lock(),
                coordinator,
                Mock(refresh=Mock(return_value={})),
            )
        recorded.assert_called_once()
        self.assertTrue(service._DASHBOARD_DATA_AVAILABLE)

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
                    "usage_tokens": float((index + 1) * 100),
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
            self.assertEqual(session["projected_next_10_usage_tokens"], 2000.0)
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

    def test_incomplete_iteration_is_omitted_without_resetting_history(self) -> None:
        rows = iteration_series(
            [1.0, 2.0, 3.0],
            [1.0, None, 3.0],
            [],
            [True, False, True],
        )
        self.assertEqual([row["cost_usd"] for row in rows], [1.0, None, 3.0])
        self.assertEqual([row["cumulative_cost_usd"] for row in rows], [1.0, 1.0, 4.0])
        self.assertEqual([row["priced"] for row in rows], [True, False, True])

    def test_configuration_baseline_matches_model_and_effort_only(self) -> None:
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
        self.assertEqual(single_model_effort([standard, fast]), ("model", "medium"))
        series = [(1.0, 1)] * 10
        samples = [
            ("model", "medium", series),
            ("model", "medium", series),
            ("model", "medium", series),
            ("model", "medium", series),
            ("model", "medium", series),
            (None, None, series),
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
            model="model",
            effort="medium",
            cost_usd=10,
            comparison_scope="model_effort",
        )
        provider = baseline_comparison(
            "codex",
            10,
            10,
            baseline,
            model="model",
            effort="medium",
            cost_usd=10,
            comparison_scope="provider",
        )
        unavailable_match = baseline_comparison(
            "codex",
            10,
            10,
            baseline,
            model="other-model",
            effort="medium",
            cost_usd=10,
            comparison_scope="model_effort",
        )
        extrapolated_match = baseline_comparison(
            "codex",
            20,
            20,
            baseline,
            model="model",
            effort="medium",
            cost_usd=20,
            comparison_scope="model_effort",
        )
        self.assertEqual(matched["scope"], "model_effort")
        self.assertEqual(provider["scope"], "provider")
        self.assertIsNone(unavailable_match)
        self.assertIsNone(extrapolated_match)
        self.assertEqual(
            baseline["forecasts"]["provider_median_next_10"]["codex"],
            {"median_next_10_usd": 10.0, "sessions": 6},
        )

    def test_snapshot_keeps_short_post_compact_model_effort_baseline(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": session_id,
                "type": "system",
                "subtype": "compact_boundary",
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "sessionId": session_id,
                "message": {"role": "user", "content": "question"},
            },
            {
                "timestamp": "2026-01-01T00:00:02Z",
                "sessionId": session_id,
                "effort": "medium",
                "message": {
                    "id": "message",
                    "role": "assistant",
                    "model": "model",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 0,
                        "speed": "fast",
                    },
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
        self.assertTrue(session["since_compact"])
        self.assertEqual(session["speed"], "fast")
        self.assertEqual(
            session["comparison_configuration"],
            {"model": "model", "effort": "medium"},
        )
        self.assertEqual(session["baseline"]["scope"], "model_effort")
        self.assertEqual(session["baselines"]["provider"]["scope"], "provider")
        self.assertEqual(
            session["baselines"]["model_effort"]["scope"],
            "model_effort",
        )

    def test_sessions_without_a_forecast_never_alert(self) -> None:
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
        sessions = [self.forecast_session(40.0)]
        sessions[0]["cost_status"] = "partial"
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "notifications.json"
            with patch(
                "konvu_telemetry.analytics.notification_state_path",
                return_value=state_path,
            ):
                apply_notification_tracking(sessions, 100.0)
        self.assertEqual(sessions[0]["notification"], {"sequence": 0, "hot": False})

    def alert_state_path(self, directory: str) -> Path:
        return Path(directory) / "notifications.json"

    def forecast_session(
        self, forecast: float, active_at: float = 100.0
    ) -> dict[str, object]:
        return {
            "id": "session",
            "provider": "claude",
            "total_cost_usd": 10.0,
            "projected_next_10_tasks_usd": forecast,
            "cost_status": "complete",
            "last_activity_at": datetime.fromtimestamp(
                active_at, timezone.utc
            ).isoformat(),
        }

    def track(self, session: dict[str, object], now: float, state_path: Path) -> int:
        with patch(
            "konvu_telemetry.analytics.notification_state_path",
            return_value=state_path,
        ):
            apply_notification_tracking([session], now)
        return int(session["notification"]["sequence"])

    def track_active(
        self, session: dict[str, object], now: float, state_path: Path
    ) -> int:
        """Track a session still spending, so its activity keeps pace with the clock."""
        session["last_activity_at"] = datetime.fromtimestamp(
            now, timezone.utc
        ).isoformat()
        return self.track(session, now, state_path)

    def test_forecast_at_or_below_threshold_does_not_alert(self) -> None:
        session = self.forecast_session(ALERT_FORECAST_USD)
        with tempfile.TemporaryDirectory() as directory:
            sequence = self.track(session, 100.0, self.alert_state_path(directory))
        self.assertEqual(sequence, 0)
        self.assertFalse(session["notification"]["hot"])

    def test_forecast_above_threshold_alerts_without_any_baseline(self) -> None:
        session = self.forecast_session(ALERT_FORECAST_USD + 0.01)
        with tempfile.TemporaryDirectory() as directory:
            sequence = self.track(session, 100.0, self.alert_state_path(directory))
        self.assertEqual(sequence, 1)
        self.assertTrue(session["notification"]["hot"])

    def test_spend_below_the_median_still_alerts_on_a_high_forecast(self) -> None:
        session = self.forecast_session(40.0)
        session["baselines"] = {"provider": {"cost_overhead_percent": -80}}
        with tempfile.TemporaryDirectory() as directory:
            sequence = self.track(session, 100.0, self.alert_state_path(directory))
        self.assertEqual(sequence, 1)
        self.assertEqual(session["notification"]["overhead_percent"], -80)

    def test_session_idle_past_the_live_window_does_not_alert(self) -> None:
        session = self.forecast_session(40.0)
        with tempfile.TemporaryDirectory() as directory:
            sequence = self.track(
                session, 100.0 + 21 * 60, self.alert_state_path(directory)
            )
        self.assertEqual(sequence, 0)
        self.assertFalse(session["notification"]["hot"])

    def test_session_without_recorded_activity_does_not_alert(self) -> None:
        session = self.forecast_session(40.0)
        del session["last_activity_at"]
        with tempfile.TemporaryDirectory() as directory:
            sequence = self.track(session, 100.0, self.alert_state_path(directory))
        self.assertEqual(sequence, 0)

    def test_sustained_forecast_renotifies_only_after_five_minutes(self) -> None:
        session = self.forecast_session(12.0)
        with tempfile.TemporaryDirectory() as directory:
            path = self.alert_state_path(directory)
            self.assertEqual(self.track_active(session, 100.0, path), 1)
            self.assertEqual(self.track_active(session, 100.0 + 4 * 60, path), 1)
            self.assertEqual(self.track_active(session, 100.0 + 5 * 60, path), 2)

    def test_forecast_brought_down_stops_renotifying_until_it_recovers(self) -> None:
        session = self.forecast_session(12.0)
        with tempfile.TemporaryDirectory() as directory:
            path = self.alert_state_path(directory)
            self.assertEqual(self.track_active(session, 100.0, path), 1)
            session["projected_next_10_tasks_usd"] = 6.0
            self.assertEqual(self.track_active(session, 100.0 + 10 * 60, path), 1)
            session["projected_next_10_tasks_usd"] = 12.0
            self.assertEqual(self.track_active(session, 100.0 + 20 * 60, path), 2)

    def test_cooling_below_threshold_rearms_the_next_crossing(self) -> None:
        session = self.forecast_session(12.0)
        with tempfile.TemporaryDirectory() as directory:
            path = self.alert_state_path(directory)
            self.assertEqual(self.track_active(session, 100.0, path), 1)
            session["projected_next_10_tasks_usd"] = 1.0
            self.assertEqual(self.track_active(session, 100.0 + 60, path), 1)
            # Re-arming forgets the $12 peak, so $5 alerts once the repeat floor passes.
            session["projected_next_10_tasks_usd"] = 5.0
            self.assertEqual(self.track_active(session, 100.0 + 5 * 60, path), 2)

    def test_forecast_oscillating_across_the_threshold_respects_the_repeat_floor(
        self,
    ) -> None:
        session = self.forecast_session(5.0)
        with tempfile.TemporaryDirectory() as directory:
            path = self.alert_state_path(directory)
            self.assertEqual(self.track_active(session, 100.0, path), 1)
            for step, forecast in enumerate((3.9, 5.0, 3.9, 5.0), start=1):
                session["projected_next_10_tasks_usd"] = forecast
                sequence = self.track_active(session, 100.0 + step * 30, path)
                self.assertEqual(sequence, 1)

    def test_forecast_borrowed_from_a_median_does_not_alert(self) -> None:
        session = self.forecast_session(40.0)
        session["forecast_basis"] = {"coverage": "historical_fallback"}
        with tempfile.TemporaryDirectory() as directory:
            sequence = self.track(session, 100.0, self.alert_state_path(directory))
        self.assertEqual(sequence, 0)
        self.assertFalse(session["notification"]["hot"])

    def test_future_activity_stamp_does_not_keep_a_session_live(self) -> None:
        session = self.forecast_session(40.0, active_at=100.0 + 10 * 60)
        with tempfile.TemporaryDirectory() as directory:
            sequence = self.track(session, 100.0, self.alert_state_path(directory))
        self.assertEqual(sequence, 0)

    def test_state_written_before_this_rule_alerts_afresh(self) -> None:
        session = self.forecast_session(12.0)
        with tempfile.TemporaryDirectory() as directory:
            path = self.alert_state_path(directory)
            path.write_text(
                json.dumps(
                    {
                        "claude:session": {
                            "sequence": 3,
                            "hot": True,
                            "last_notified_at": 100.0,
                            "last_cost_usd": 9.0,
                            "last_overhead_percent": 250,
                        }
                    }
                )
            )
            # The recorded clock still binds, so the upgrade cannot alert immediately.
            self.assertEqual(self.track_active(session, 100.0 + 60, path), 3)
            self.assertEqual(self.track_active(session, 100.0 + 5 * 60, path), 4)

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
