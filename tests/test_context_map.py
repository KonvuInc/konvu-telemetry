from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from konvu_telemetry.context_drift import _event_text
from konvu_telemetry.context_map import (
    ContextMapCollector,
    ContextMapScheduler,
    STATE_VERSION,
    _category,
)
from konvu_telemetry.context_tokenizer import ContextTokenizer
from konvu_telemetry.live import ClaudeLiveFile, CodexLiveFile, IncrementalLiveState
from konvu_telemetry.storage import context_map_path


SESSION_ID = "00000000-0000-0000-0000-000000000001"


class FixedTokenizer(ContextTokenizer):
    def count(self, provider: str, model: str, value: object) -> tuple[int, str]:
        if value is None:
            return 0, f"fixed-{provider}-{model}"
        return 10, f"fixed-{provider}-{model}"


class RecordingEncoding:
    def __init__(self) -> None:
        self.disallowed_special: tuple[object, ...] | None = None

    def encode(
        self, _content: str, *, disallowed_special: tuple[object, ...]
    ) -> list[int]:
        self.disallowed_special = disallowed_special
        return [1]


class RecordingTiktoken:
    def __init__(self, encoding: RecordingEncoding) -> None:
        self.encoding = encoding

    def get_encoding(self, _name: str) -> RecordingEncoding:
        return self.encoding


class RecordingCtok:
    def __init__(self) -> None:
        self.versions: list[str] = []

    def token_count(self, _content: str, *, version: str) -> int:
        self.versions.append(version)
        return 7


def append_records(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def snapshot(provider: str, context_tokens: int) -> dict[str, object]:
    return {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "sessions": [
            {
                "id": SESSION_ID,
                "provider": provider,
                "context_tokens": context_tokens,
            }
        ],
    }


def live_state(provider: str, transcript: Path) -> IncrementalLiveState:
    state = IncrementalLiveState()
    if provider == "claude":
        state.claude[transcript] = ClaudeLiveFile(session_id=SESSION_ID)
    else:
        state.codex[transcript] = CodexLiveFile(session_id=SESSION_ID)
    return state


def claude_assistant(
    timestamp: str,
    input_tokens: int,
    output_tokens: int,
    *,
    model: str = "claude-opus-5",
    content: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "sessionId": SESSION_ID,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": output_tokens,
            },
            "content": content or [],
        },
    }


def codex_checkpoint(
    timestamp: str, input_tokens: int, output_tokens: int
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            },
        },
    }


def stored_events(provider: str) -> list[dict[str, object]]:
    stored = json.loads(context_map_path(provider, SESSION_ID).read_text())
    return stored["epochs"][-1]["events"]


class ContextMapTests(unittest.TestCase):
    def test_scheduler_runs_only_when_transcript_bytes_change(self) -> None:
        calls: list[dict[str, object]] = []

        def runner(payload: dict[str, object]) -> list[dict[str, object]]:
            calls.append(payload)
            sources = payload.get("sources")
            assert isinstance(sources, list)
            source = sources[0]
            assert isinstance(source, dict)
            path = Path(str(source["path"]))
            stat = path.stat()
            sessions = payload["snapshot"]["sessions"]  # type: ignore[index]
            observed = sessions[0]["context_tokens"]  # type: ignore[index]
            return [
                {
                    "provider": source["provider"],
                    "session_id": source["session_id"],
                    "source_path": str(path),
                    "source_device": stat.st_dev,
                    "source_inode": stat.st_ino,
                    "cursor": stat.st_size,
                    "summary": {
                        "state": "ready",
                        "observed_context_tokens": observed,
                    },
                }
            ]

        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            state = live_state("codex", transcript)
            scheduler = ContextMapScheduler(runner)
            first = snapshot("codex", 100)
            scheduler.refresh(first, state)
            second = snapshot("codex", 100)
            scheduler.refresh(second, state)
            updated = snapshot("codex", 101)
            scheduler.refresh(updated, state)
            transcript.write_text("{}\n{}\n", encoding="utf-8")
            scheduler.refresh(snapshot("codex", 101), state)

        self.assertEqual(len(calls), 3)
        expected = {"state": "ready", "observed_context_tokens": 100}
        self.assertEqual(first["sessions"][0]["context_map"], expected)
        self.assertEqual(second["sessions"][0]["context_map"], expected)
        self.assertEqual(
            updated["sessions"][0]["context_map"],
            {"state": "ready", "observed_context_tokens": 101},
        )

    def test_scheduler_keeps_cached_map_when_refresh_worker_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [codex_checkpoint("2026-01-01T00:00:00Z", 100, 5)],
            )
            state = live_state("codex", transcript)
            data = snapshot("codex", 100)
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(data, state)
                expected = data["sessions"][0]["context_map"]  # type: ignore[index]
                append_records(transcript, [{"type": "unrecognized"}])
                scheduler = ContextMapScheduler(
                    lambda _payload: (_ for _ in ()).throw(TimeoutError())
                )
                refreshed = snapshot("codex", 100)
                with self.assertLogs("konvu_telemetry.context_map", level="WARNING"):
                    scheduler.refresh(refreshed, state)

            self.assertEqual(refreshed["sessions"][0]["context_map"], expected)  # type: ignore[index]

    def test_codex_special_tokens_are_counted_as_transcript_text(self) -> None:
        tokenizer = ContextTokenizer()
        encoding = RecordingEncoding()
        tokenizer._tiktoken = RecordingTiktoken(encoding)

        for model in ("gpt-5.6", "gpt-6-sol", "gpt-6-astra"):
            count, name = tokenizer.count("codex", model, "<|endoftext|>")

            self.assertEqual((count, name), (1, "o200k_base"))
        self.assertEqual(encoding.disallowed_special, ())

    def test_claude_models_select_the_pinned_tokenizer_family(self) -> None:
        tokenizer = ContextTokenizer()
        recorder = RecordingCtok()
        tokenizer._ctok = recorder

        versions = []
        for model in (
            "claude-opus-4-5",
            "claude-sonnet-4.7",
            "claude-opus-4-8",
            "claude-opus-5-5",
            "claude-opus-5.5",
            "claude-newfamily-5-1",
            "claude-opus-4-20250514",
        ):
            count, name = tokenizer.count("claude", model, "context")
            self.assertEqual(count, 7)
            versions.append(name)

        self.assertEqual(
            versions,
            [
                "ctok-3.0",
                "ctok-4.7",
                "ctok-4.8",
                "ctok-5.0",
                "ctok-5.0",
                "ctok-5.0",
                "ctok-3.0",
            ],
        )

    def test_claude_tokenizer_failure_never_falls_back_to_byte_estimates(self) -> None:
        tokenizer = ContextTokenizer()
        with (
            patch.object(tokenizer._ctok, "token_count", side_effect=RuntimeError()),
            self.assertRaises(RuntimeError),
        ):
            tokenizer.count("claude", "claude-opus-5-5", "context")

    def test_claude_commits_only_complete_checkpoint_windows(self) -> None:
        secret = "raw-result-must-not-be-copied"
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "sessionId": SESSION_ID,
                        "type": "user",
                        "message": {"role": "user", "content": "prompt"},
                    },
                    claude_assistant(
                        "2026-01-01T00:00:01Z",
                        100,
                        10,
                        content=[
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Read",
                                "input": {"file_path": "/repo/src/app.py"},
                            }
                        ],
                    ),
                    {
                        "timestamp": "2026-01-01T00:00:02Z",
                        "sessionId": SESSION_ID,
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call-1",
                                    "content": secret,
                                }
                            ],
                        },
                    },
                ],
            )
            state = live_state("claude", transcript)
            data = snapshot("claude", 110)
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                collector = ContextMapCollector(FixedTokenizer())
                collector.refresh(data, state)
                first_state = json.loads(
                    context_map_path("claude", SESSION_ID).read_text()
                )
                self.assertEqual(len(first_state["pending_sources"]), 1)
                append_records(
                    transcript,
                    [claude_assistant("2026-01-01T00:00:03Z", 160, 5)],
                )
                collector.refresh(data, state)
                collector.refresh(data, state)
                summary = data["sessions"][0]["context_map"]  # type: ignore[index]
                stored = context_map_path("claude", SESSION_ID).read_text()
                final_state = json.loads(stored)
            self.assertEqual(final_state["pending_sources"], [])
            self.assertEqual(final_state["epochs"][-1]["checkpoint_count"], 2)
            self.assertGreater(summary["categories"]["repository_and_files"], 0)  # type: ignore[index]
            self.assertNotIn(secret, stored)
            self.assertIn("src/app.py", stored)

    def test_claude_synthetic_messages_do_not_erase_the_last_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    claude_assistant("2026-01-01T00:00:00Z", 184_674, 81),
                    claude_assistant(
                        "2026-01-01T00:00:01Z",
                        0,
                        0,
                        model="<synthetic>",
                    ),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("claude", 184_674), live_state("claude", transcript)
                )
                stored = json.loads(context_map_path("claude", SESSION_ID).read_text())
            summary = stored["summary"]
            self.assertEqual(summary["observed_context_tokens"], 184_674)
            self.assertEqual(stored["active_model"], "claude-opus-5")

    def test_old_context_map_is_rebuilt_after_parser_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [claude_assistant("2026-01-01T00:00:00Z", 120, 5)],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                destination = context_map_path("claude", SESSION_ID)
                destination.parent.mkdir(parents=True)
                destination.write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "cursor": transcript.stat().st_size,
                            "epochs": [{"observed_context_tokens": 0}],
                        }
                    )
                )
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("claude", 120), live_state("claude", transcript)
                )
                stored = json.loads(destination.read_text())
            self.assertEqual(stored["version"], STATE_VERSION)
            self.assertEqual(stored["summary"]["observed_context_tokens"], 120)

    def test_parser_rebuild_preserves_analysis_for_the_same_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [claude_assistant("2026-01-01T00:00:00Z", 120, 5)],
            )
            stat = transcript.stat()
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                destination = context_map_path("claude", SESSION_ID)
                destination.parent.mkdir(parents=True)
                destination.write_text(
                    json.dumps(
                        {
                            "version": STATE_VERSION - 1,
                            "source_path": str(transcript),
                            "source_device": stat.st_dev,
                            "source_inode": stat.st_ino,
                            "epochs": [{}],
                            "analysis": {
                                "version": 3,
                                "state": "ready",
                                "items": {},
                            },
                        }
                    )
                )
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("claude", 120), live_state("claude", transcript)
                )
                stored = json.loads(destination.read_text())

            self.assertEqual(stored["version"], STATE_VERSION)
            self.assertEqual(stored["analysis"]["version"], 3)

    def test_codex_uses_the_next_checkpoint_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.6-sol"},
                    },
                    codex_checkpoint("2026-01-01T00:00:01Z", 100, 10),
                    {
                        "timestamp": "2026-01-01T00:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "web_search",
                            "arguments": json.dumps({"url": "https://example.com/a"}),
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "search result",
                        },
                    },
                    codex_checkpoint("2026-01-01T00:00:04Z", 140, 5),
                    codex_checkpoint("2026-01-01T00:00:05Z", 175, 2),
                ],
            )
            state = live_state("codex", transcript)
            data = snapshot("codex", 175)
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                collector = ContextMapCollector(FixedTokenizer())
                collector.refresh(data, state)
                first = data["sessions"][0]["context_map"]  # type: ignore[index]
                collector.refresh(data, state)
                second = data["sessions"][0]["context_map"]  # type: ignore[index]
                events = stored_events("codex")
            self.assertEqual(first, second)
            web = [event for event in events if event["category"] == "web_and_external"]
            self.assertEqual(web[0]["estimated_tokens"], 30)
            self.assertEqual(web[0]["model"], "gpt-5.6-sol")

    def test_reply_excerpts_point_at_reply_records_not_usage_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            thinking = claude_assistant(
                "2026-01-01T00:00:01Z", 100, 20, content=[{"type": "thinking", "thinking": ""}]
            )
            reply = claude_assistant(
                "2026-01-01T00:00:02Z", 100, 20, content=[{"type": "text", "text": "Fixed the cap."}]
            )
            following = claude_assistant("2026-01-01T00:00:03Z", 140, 5)
            for record, message_id in ((thinking, "msg-1"), (reply, "msg-1"), (following, "msg-2")):
                record["message"]["id"] = message_id  # type: ignore[index]
            append_records(transcript, [thinking, reply, following])
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("claude", 140), live_state("claude", transcript)
                )
                events = stored_events("claude")
            output = next(e for e in events if e["category"] == "assistant_output")
            self.assertEqual(len(output["excerpt_ranges"]), 1)
            self.assertEqual(_event_text(output, transcript), "Fixed the cap.")

    def test_codex_reply_and_call_offsets_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    codex_checkpoint("2026-01-01T00:00:01Z", 100, 10),
                    {
                        "timestamp": "2026-01-01T00:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "pytest -q"}),
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "3 passed",
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:04Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Tests pass."}],
                        },
                    },
                    codex_checkpoint("2026-01-01T00:00:05Z", 140, 5),
                    codex_checkpoint("2026-01-01T00:00:06Z", 170, 2),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("codex", 170), live_state("codex", transcript)
                )
                events = stored_events("codex")
            result = next(e for e in events if e.get("tool") == "exec_command")
            replies = [e for e in events if e.get("excerpt_ranges")]
            self.assertEqual(len(result["call_range"]), 2)
            self.assertEqual(len(replies), 1)

    def test_codex_tracks_whether_the_current_turn_finished(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    },
                    codex_checkpoint("2026-01-01T00:00:01Z", 100, 5),
                ],
            )
            state = live_state("codex", transcript)
            data = snapshot("codex", 100)
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                collector = ContextMapCollector(FixedTokenizer())
                collector.refresh(data, state)
                running = json.loads(context_map_path("codex", SESSION_ID).read_text())
                append_records(
                    transcript,
                    [
                        {
                            "timestamp": "2026-01-01T00:00:02Z",
                            "type": "event_msg",
                            "payload": {"type": "task_complete"},
                        }
                    ],
                )
                collector.refresh(data, state)
                complete = json.loads(context_map_path("codex", SESSION_ID).read_text())

            self.assertFalse(running["turn_complete"])
            self.assertFalse(running["summary"]["turn_complete"])
            self.assertTrue(complete["turn_complete"])
            self.assertTrue(complete["summary"]["turn_complete"])

    def test_codex_tool_output_is_attributed_at_the_following_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    codex_checkpoint("2026-01-01T00:00:00Z", 100, 5),
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "tool output",
                        },
                    },
                    codex_checkpoint("2026-01-01T00:00:02Z", 105, 0),
                    codex_checkpoint("2026-01-01T00:00:03Z", 125, 0),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("codex", 125), live_state("codex", transcript)
                )
                stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
            categories = stored["epochs"][-1]["categories"]
            self.assertEqual(categories["other_tool_output"], 20)
            self.assertEqual(categories.get("provider_internal", 0), 0)

    def test_claude_deduplicates_split_messages_and_accounts_for_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            first = claude_assistant("2026-01-01T00:00:00Z", 100, 10)
            first["message"]["id"] = "message-1"  # type: ignore[index]
            duplicate = claude_assistant("2026-01-01T00:00:01Z", 100, 10)
            duplicate["message"]["id"] = "message-1"  # type: ignore[index]
            second = claude_assistant("2026-01-01T00:00:02Z", 130, 5)
            second["message"]["id"] = "message-2"  # type: ignore[index]
            append_records(transcript, [first, duplicate, second])
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("claude", 130), live_state("claude", transcript)
                )
                stored = json.loads(context_map_path("claude", SESSION_ID).read_text())
            epoch = stored["epochs"][-1]
            self.assertEqual(epoch["checkpoint_count"], 2)
            self.assertEqual(epoch["categories"]["assistant_output"], 10)
            self.assertEqual(sum(epoch["categories"].values()), 130)

    def test_claude_meta_context_is_kept_as_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            meta = {
                "timestamp": "2026-01-01T00:00:01Z",
                "sessionId": SESSION_ID,
                "type": "user",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "injected skill"}],
                },
            }
            append_records(
                transcript,
                [
                    claude_assistant("2026-01-01T00:00:00Z", 100, 5),
                    meta,
                    claude_assistant("2026-01-01T00:00:02Z", 115, 2),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("claude", 115), live_state("claude", transcript)
                )
                stored = json.loads(context_map_path("claude", SESSION_ID).read_text())
            categories = stored["epochs"][-1]["categories"]
            self.assertEqual(categories["skills_and_instructions"], 10)
            self.assertEqual(categories.get("provider_internal", 0), 0)

    def test_claude_attachment_context_is_not_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            attachment = {
                "timestamp": "2026-01-01T00:00:01Z",
                "sessionId": SESSION_ID,
                "type": "attachment",
                "attachment": {
                    "type": "skill_listing",
                    "content": "available skills",
                },
            }
            append_records(
                transcript,
                [
                    claude_assistant("2026-01-01T00:00:00Z", 100, 5),
                    attachment,
                    claude_assistant("2026-01-01T00:00:02Z", 115, 2),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("claude", 115), live_state("claude", transcript)
                )
                stored = json.loads(context_map_path("claude", SESSION_ID).read_text())
            categories = stored["epochs"][-1]["categories"]
            self.assertEqual(categories["skills_and_instructions"], 10)
            self.assertEqual(categories.get("provider_internal", 0), 0)

    def test_claude_tool_search_residual_is_loaded_tool_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            first = claude_assistant(
                "2026-01-01T00:00:00Z",
                100,
                5,
                content=[
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "ToolSearch",
                        "input": {"query": "select:Read"},
                    }
                ],
            )
            result = {
                "timestamp": "2026-01-01T00:00:01Z",
                "sessionId": SESSION_ID,
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-1",
                            "content": "loaded",
                        }
                    ],
                },
            }
            append_records(
                transcript,
                [first, result, claude_assistant("2026-01-01T00:00:02Z", 200, 2)],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("claude", 200), live_state("claude", transcript)
                )
                stored = json.loads(context_map_path("claude", SESSION_ID).read_text())
            categories = stored["epochs"][-1]["categories"]
            self.assertEqual(categories["skills_and_instructions"], 95)
            self.assertEqual(categories.get("provider_internal", 0), 0)

    def test_claude_tools_changed_diagnostic_categorizes_hidden_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            first = claude_assistant(
                "2026-01-01T00:00:00Z",
                100,
                5,
                content=[
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "Read",
                        "input": {"file_path": "src/app.py"},
                    }
                ],
            )
            result = {
                "timestamp": "2026-01-01T00:00:01Z",
                "sessionId": SESSION_ID,
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-1",
                            "content": "file contents",
                        }
                    ],
                },
            }
            second = claude_assistant("2026-01-01T00:00:02Z", 200, 2)
            second["message"]["diagnostics"] = {  # type: ignore[index]
                "cache_miss_reason": {"type": "tools_changed"}
            }
            append_records(transcript, [first, result, second])
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("claude", 200), live_state("claude", transcript)
                )
                stored = json.loads(context_map_path("claude", SESSION_ID).read_text())
            categories = stored["epochs"][-1]["categories"]
            self.assertGreater(categories["skills_and_instructions"], 0)
            self.assertEqual(categories.get("provider_internal", 0), 0)

    def test_codex_modern_prompt_is_categorized_and_fully_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.6-sol"},
                    },
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "prompt"}],
                        },
                    },
                    codex_checkpoint("2026-01-01T00:00:02Z", 100, 10),
                    codex_checkpoint("2026-01-01T00:00:03Z", 130, 5),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("codex", 130), live_state("codex", transcript)
                )
                stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
            categories = stored["epochs"][-1]["categories"]
            self.assertGreater(categories["prompts"], 0)
            self.assertEqual(categories["assistant_output"], 10)
            self.assertEqual(sum(categories.values()), 130)

    def test_codex_injected_user_message_is_session_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "# AGENTS.md instructions\nUse local rules.",
                                }
                            ],
                        },
                    },
                    codex_checkpoint("2026-01-01T00:00:01Z", 100, 5),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("codex", 100), live_state("codex", transcript)
                )
                categories = json.loads(
                    context_map_path("codex", SESSION_ID).read_text()
                )["epochs"][-1]["categories"]

            self.assertGreater(categories["skills_and_instructions"], 0)
            self.assertNotIn("prompts", categories)

    def test_codex_duplicate_checkpoint_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            checkpoint = codex_checkpoint("2026-01-01T00:00:00Z", 100, 10)
            append_records(transcript, [checkpoint, checkpoint])
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("codex", 100), live_state("codex", transcript)
                )
                stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
            self.assertEqual(stored["epochs"][-1]["checkpoint_count"], 1)
            self.assertEqual(sum(stored["epochs"][-1]["categories"].values()), 100)

    def test_model_switch_only_affects_new_additions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "sessionId": SESSION_ID,
                        "type": "user",
                        "message": {"role": "user", "content": "first"},
                    },
                    claude_assistant(
                        "2026-01-01T00:00:01Z",
                        100,
                        10,
                        model="claude-opus-5",
                        content=[
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Read",
                                "input": {"file_path": "/repo/a.py"},
                            }
                        ],
                    ),
                    {
                        "timestamp": "2026-01-01T00:00:02Z",
                        "sessionId": SESSION_ID,
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call-1",
                                    "content": "result",
                                }
                            ],
                        },
                    },
                    claude_assistant(
                        "2026-01-01T00:00:03Z",
                        120,
                        4,
                        model="claude-sonnet-4-5",
                    ),
                ],
            )
            state = live_state("claude", transcript)
            data = snapshot("claude", 120)
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(data, state)
                events = stored_events("claude")
            prompt = next(event for event in events if event["category"] == "prompts")
            file_event = next(
                event for event in events if event["category"] == "repository_and_files"
            )
            self.assertEqual(prompt["model"], "claude-opus-5")
            self.assertEqual(file_event["model"], "claude-sonnet-4-5")
            self.assertEqual(file_event["confidence"], "estimated_model_switch")

    def test_codex_model_switch_keeps_the_source_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.6-sol"},
                    },
                    codex_checkpoint("2026-01-01T00:00:01Z", 100, 5),
                    {
                        "timestamp": "2026-01-01T00:00:02Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "prompt"},
                    },
                    codex_checkpoint("2026-01-01T00:00:03Z", 120, 5),
                    {
                        "timestamp": "2026-01-01T00:00:04Z",
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.6-terra"},
                    },
                    codex_checkpoint("2026-01-01T00:00:05Z", 130, 2),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("codex", 130), live_state("codex", transcript)
                )
                prompt = next(
                    event
                    for event in stored_events("codex")
                    if event["category"] == "prompts"
                )
            self.assertEqual(prompt["model"], "gpt-5.6-sol")
            self.assertEqual(prompt["confidence"], "measured_window")

    def test_claude_image_formula_is_reconciled_to_the_checkpoint(self) -> None:
        png = bytearray(24)
        png[:8] = b"\x89PNG\r\n\x1a\n"
        png[16:20] = (56).to_bytes(4, "big")
        png[20:24] = (56).to_bytes(4, "big")
        encoded = base64.b64encode(png).decode()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    claude_assistant(
                        "2026-01-01T00:00:00Z",
                        100,
                        10,
                        content=[
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "View",
                                "input": {"file_path": "/tmp/a.png"},
                            }
                        ],
                    ),
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "sessionId": SESSION_ID,
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call-1",
                                    "content": [
                                        {
                                            "type": "image",
                                            "source": {
                                                "type": "base64",
                                                "media_type": "image/png",
                                                "data": encoded,
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                    claude_assistant("2026-01-01T00:00:02Z", 114, 1),
                ],
            )
            state = live_state("claude", transcript)
            data = snapshot("claude", 114)
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(data, state)
                events = stored_events("claude")
            image = next(event for event in events if event["category"] == "images")
            self.assertEqual(image["estimated_tokens"], 4)
            self.assertEqual(image["tokenizer"], "claude-image-grid")

    def test_categories_are_deterministic_from_type_tool_and_arguments(self) -> None:
        self.assertEqual(_category("Read", "docs/report.pdf"), "documents")
        self.assertEqual(_category("Read", "docs/spec.docx"), "documents")
        self.assertEqual(_category("View", "shots/a.png"), "images")
        self.assertEqual(_category("web_search", "example.com/a"), "web_and_external")
        self.assertEqual(_category("mcp__db__query", "query"), "production_systems")
        self.assertEqual(_category("Read", "src/app.py"), "repository_and_files")
        self.assertEqual(_category("apply_patch", "patch"), "file_changes")
        self.assertEqual(
            _category("exec_command", "exec_command", {"cmd": "npm test"}),
            "tests_and_builds",
        )
        self.assertEqual(
            _category("exec_command", "exec_command", {"cmd": "cat > src/app.py"}),
            "file_changes",
        )
        self.assertEqual(
            _category("Read", "src/webhook.py", {"path": "src/webhook.py"}),
            "repository_and_files",
        )
        self.assertEqual(
            _category(
                "Read", "docs/instructions.txt", {"path": "docs/instructions.txt"}
            ),
            "documents",
        )
        self.assertEqual(
            _category("write_stdin", "write_stdin", {"chars": "yes"}),
            "local_system_data",
        )
        self.assertEqual(_category("wait", "wait"), "subagent_handoffs")
        self.assertEqual(
            _category("js", "js", {"code": "await tab.getAXState()"}),
            "web_and_external",
        )
        self.assertEqual(
            _category("js", "js", {"code": 'await cua.getApp("Granola")'}),
            "local_system_data",
        )
        self.assertEqual(_category("queued_command", "Queued Command"), "prompts")
        self.assertEqual(_category("AskUserQuestion", "AskUserQuestion"), "prompts")
        self.assertEqual(
            _category("prompt_snapshot", "Prompt Snapshot"),
            "skills_and_instructions",
        )
        for tool in (
            "agent_listing_delta",
            "auto_mode",
            "bash_output_audience_note",
            "command_permissions",
            "credential_org",
            "date",
            "date_change",
            "model",
            "remote_session_change",
            "session_context",
        ):
            with self.subTest(tool=tool):
                self.assertEqual(_category(tool, tool), "skills_and_instructions")
        self.assertEqual(
            _category("edited_text_file", "Edited Text File"), "file_changes"
        )
        self.assertEqual(
            _category("file", "File", {"filename": "src/app.py"}),
            "repository_and_files",
        )
        self.assertEqual(_category("Artifact", "Artifact"), "file_changes")
        self.assertEqual(
            _category("compact_file_reference", "Compact File Reference"),
            "previous_compact",
        )
        self.assertEqual(
            _category("list_mcp_resources", "list_mcp_resources"),
            "skills_and_instructions",
        )
        self.assertEqual(
            _category(
                "read_mcp_resource",
                "read_mcp_resource",
                {"server": "datadog", "uri": "logs://service"},
            ),
            "production_systems",
        )
        self.assertEqual(
            _category("mcp__slack__search", "search"), "external_service_data"
        )
        self.assertEqual(
            _category("mcp__claude_ai_Brex__list_expenses", "expenses"),
            "external_service_data",
        )
        self.assertEqual(
            _category("mcp__unknown__read", "read"), "external_service_data"
        )
        self.assertEqual(
            _category("exec_command", "exec_command", {"cmd": "rg TODO src"}),
            "repository_and_files",
        )
        self.assertEqual(
            _category("exec_command", "exec_command", {"cmd": "tail app.log"}),
            "local_logs",
        )
        self.assertEqual(
            _category("exec_command", "exec_command", {"cmd": "kubectl get pods"}),
            "production_systems",
        )
        self.assertEqual(
            _category(
                "exec_command",
                "exec_command",
                {"cmd": "curl https://example.com/docs"},
            ),
            "web_and_external",
        )
        self.assertEqual(_category("list_agents", "list_agents"), "subagent_handoffs")
        self.assertEqual(
            _category("image_gen__imagegen", "image_gen · imagegen"), "images"
        )
        self.assertEqual(
            _category("exec", "web · run", "await tools.web__run({})"),
            "web_and_external",
        )
        self.assertEqual(
            _category("exec", "apply_patch", "await tools.apply_patch('x')"),
            "file_changes",
        )

    def test_compaction_closes_history_and_starts_from_retained_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    claude_assistant("2026-01-01T00:00:00Z", 100, 5),
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "timestamp": "2026-01-01T00:01:00Z",
                        "sessionId": SESSION_ID,
                        "compactMetadata": {"preTokens": 105, "postTokens": 20},
                    },
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "timestamp": "2026-01-01T00:02:00Z",
                        "sessionId": SESSION_ID,
                        "compactMetadata": {"preTokens": 40, "postTokens": 10},
                    },
                ],
            )
            state = live_state("claude", transcript)
            data = snapshot("claude", 10)
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(data, state)
                stored = json.loads(context_map_path("claude", SESSION_ID).read_text())
            self.assertEqual(len(stored["epochs"]), 3)
            self.assertEqual(stored["epochs"][0]["observed_context_tokens"], 105)
            self.assertEqual(
                stored["epochs"][1]["categories"],
                {"previous_compact": 20, "provider_internal": 20},
            )
            self.assertEqual(
                stored["epochs"][2]["categories"], {"previous_compact": 10}
            )

    def test_codex_compaction_starts_a_new_reconciled_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    codex_checkpoint("2026-01-01T00:00:00Z", 100, 5),
                    {
                        "timestamp": "2026-01-01T00:01:00Z",
                        "type": "compacted",
                        "payload": {"replacement_history": "retained summary"},
                    },
                    codex_checkpoint("2026-01-01T00:01:00.1Z", 0, 0),
                    codex_checkpoint("2026-01-01T00:01:01Z", 25, 2),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("codex", 25), live_state("codex", transcript)
                )
                stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
            self.assertEqual(len(stored["epochs"]), 2)
            categories = stored["epochs"][1]["categories"]
            self.assertEqual(categories["previous_compact"], 10)
            self.assertEqual(sum(categories.values()), 25)

    def test_newer_session_snapshot_does_not_mutate_transcript_attribution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [codex_checkpoint("2026-01-01T00:00:00Z", 100, 5)],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("codex", 150), live_state("codex", transcript)
                )
                stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
            epoch = stored["epochs"][-1]
            self.assertEqual(epoch["observed_context_tokens"], 100)
            self.assertEqual(epoch["session_context_tokens"], 150)
            self.assertEqual(sum(epoch["categories"].values()), 100)
            checkpoints = {event["checkpoint_id"] for event in epoch["events"]}
            self.assertNotIn("session-snapshot", checkpoints)

    def test_pending_call_survives_restart_without_persisting_arguments(self) -> None:
        secret = "private-command-argument"
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "call_id": "call-1",
                            "name": "exec",
                            "input": json.dumps({"command": secret}),
                        },
                    }
                ],
            )
            state = live_state("codex", transcript)
            data = snapshot("codex", 100)
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(data, state)
                stored = context_map_path("codex", SESSION_ID).read_text()
            self.assertNotIn(secret, stored)
            self.assertIn("call-1", stored)

    def test_file_write_arguments_are_attributed_without_double_counting(self) -> None:
        secret = "private-patch-content"
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    codex_checkpoint("2026-01-01T00:00:00Z", 100, 40),
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "call_id": "call-1",
                            "name": "exec",
                            "input": f"await tools.apply_patch('{secret}')",
                        },
                    },
                    codex_checkpoint("2026-01-01T00:00:02Z", 140, 0),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("codex", 140), live_state("codex", transcript)
                )
                stored = json.loads(context_map_path("codex", SESSION_ID).read_text())

            categories = stored["epochs"][-1]["categories"]
            self.assertEqual(categories["file_changes"], 10)
            self.assertEqual(categories["assistant_output"], 30)
            self.assertEqual(sum(categories.values()), 140)
            self.assertNotIn(secret, json.dumps(stored))

    def test_claude_file_write_arguments_are_attributed_without_double_counting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"{SESSION_ID}.jsonl"
            append_records(
                transcript,
                [
                    claude_assistant(
                        "2026-01-01T00:00:00Z",
                        100,
                        40,
                        content=[
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "apply_patch",
                                "input": {"patch": "private-patch-content"},
                            }
                        ],
                    ),
                    claude_assistant("2026-01-01T00:00:01Z", 140, 0),
                ],
            )
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                ContextMapCollector(FixedTokenizer()).refresh(
                    snapshot("claude", 140), live_state("claude", transcript)
                )
                stored = json.loads(context_map_path("claude", SESSION_ID).read_text())

            categories = stored["epochs"][-1]["categories"]
            self.assertEqual(categories["file_changes"], 10)
            self.assertEqual(categories["assistant_output"], 30)
            self.assertEqual(sum(categories.values()), 140)

    def test_partial_record_waits_and_replaced_file_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            complete = json.dumps(codex_checkpoint("2026-01-01T00:00:00Z", 100, 5))
            transcript.write_text(complete[:-5], encoding="utf-8")
            state = live_state("codex", transcript)
            data = snapshot("codex", 100)
            with patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}):
                collector = ContextMapCollector(FixedTokenizer())
                collector.refresh(data, state)
                stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
                self.assertEqual(stored["cursor"], 0)
                transcript.write_text(complete + "\n", encoding="utf-8")
                collector.refresh(data, state)
                stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
                self.assertEqual(stored["cursor"], transcript.stat().st_size)
                replacement = Path(directory) / "replacement.jsonl"
                append_records(
                    replacement,
                    [codex_checkpoint("2026-01-01T00:01:00Z", 50, 2)],
                )
                replacement.replace(transcript)
                data["sessions"][0]["context_tokens"] = 50  # type: ignore[index]
                collector.refresh(data, state)
                stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
            self.assertEqual(stored["epochs"][0]["observed_context_tokens"], 50)

    def test_oversized_record_advances_cursor_without_copying_raw_data(self) -> None:
        secret = "oversized-private-output"
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / f"rollout-{SESSION_ID}.jsonl"
            transcript.write_text(
                json.dumps({"payload": {"output": secret * 20}}) + "\n",
                encoding="utf-8",
            )
            state = live_state("codex", transcript)
            data = snapshot("codex", 1_000)
            with (
                patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": directory}),
                patch("konvu_telemetry.context_map.MAX_CONTEXT_RECORD_BYTES", 64),
            ):
                ContextMapCollector(FixedTokenizer()).refresh(data, state)
                stored_text = context_map_path("codex", SESSION_ID).read_text()
                stored = json.loads(stored_text)
            self.assertNotIn(secret, stored_text)
            self.assertEqual(stored["cursor"], transcript.stat().st_size)
            self.assertEqual(len(stored["pending_sources"]), 1)


if __name__ == "__main__":
    unittest.main()
