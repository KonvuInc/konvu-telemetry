from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import Mock, patch

from konvu_telemetry import context_drift
from konvu_telemetry.context_drift import ContextDriftScheduler, LocalCliAnalysisRunner
from konvu_telemetry.preferences import read_preferences, write_preferences
from konvu_telemetry.storage import (
    analysis_usage_path,
    context_map_path,
    write_private_json,
)


SESSION_ID = "01a041b4-30c4-7e70-a9f6-19df219a6643"
NOW = 1_800_000_000.0


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class ContextDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": self.directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_preferences("every-tool-call", context_analysis_enabled=True)
        self.transcript = Path(self.directory.name) / "session.jsonl"
        self.events = self.write_transcript()
        self.write_context_map()

    def write_transcript(self) -> list[dict[str, object]]:
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": f"Investigate context drift part {index}",
                },
            }
            for index in range(1, 11)
        ]
        raw = b""
        events = []
        for index, record in enumerate(records, start=1):
            line = json.dumps(record).encode() + b"\n"
            start = len(raw)
            raw += line
            events.append(
                {
                    "id": f"event-{index}",
                    "category": "prompts",
                    "label": "User prompt",
                    "iteration": index,
                    "estimated_tokens": 10,
                    "source_start": start,
                    "source_end": len(raw),
                }
            )
        self.transcript.write_bytes(raw)
        return events

    def write_context_map(self) -> None:
        write_private_json(
            context_map_path("codex", SESSION_ID),
            {
                "version": 15,
                "provider": "codex",
                "session_id": SESSION_ID,
                "source_path": str(self.transcript),
                "current_epoch": 0,
                "iteration": 10,
                "epochs": [
                    {
                        "index": 0,
                        "events": self.events,
                        "categories": {"prompts": 100},
                    }
                ],
                "summary": {
                    "state": "ready",
                    "iteration": 10,
                    "observed_context_tokens": 100,
                    "categories": {"prompts": 100},
                },
            },
        )

    @staticmethod
    def snapshot(mode: str = "included") -> dict[str, object]:
        return {
            "sessions": [
                {
                    "provider": "codex",
                    "id": SESSION_ID,
                    "usage_mode": mode,
                    "last_activity_at": iso(NOW),
                    "context_map": {
                        "state": "ready",
                        "iteration": 10,
                        "categories": {"prompts": 100},
                    },
                }
            ]
        }

    @staticmethod
    def quotas(
        used: float = 20.0,
        observed_at: float = NOW,
        resets_at: str = "2030-01-01T00:00:00+00:00",
    ) -> dict[str, object]:
        return {
            "codex": {
                "observed_at": iso(observed_at),
                "windows": [
                    {
                        "period": "weekly",
                        "limit_id": "default",
                        "used_percent": used,
                        "resets_at": resets_at,
                    }
                ],
            }
        }

    @staticmethod
    def outcome(provider: str, payload: dict[str, object]) -> dict[str, object]:
        items = payload["items"]
        assert isinstance(items, list)
        return {
            "result": {
                "current_intent": "Build conversation drift analysis",
                "phases": [
                    {
                        "label": "Context drift",
                        "summary": "The session is implementing the classifier.",
                        "start_iteration": 1,
                        "end_iteration": None,
                    }
                ],
                "items": [
                    {
                        "id": item["id"],
                        "summary": "Context drift implementation",
                        "ai_topic": "Context analysis implementation",
                        "relevance": 0.9,
                    }
                    for item in items
                    if isinstance(item, dict)
                ],
            },
            "model": "gpt-6-luna",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "duration_seconds": 0.1,
            "error": None,
        }

    def run_scheduler(
        self, scheduler: ContextDriftScheduler, snapshot: dict[str, object]
    ) -> None:
        scheduler.refresh(snapshot, self.quotas(), NOW)
        self.assertTrue(scheduler.wait_for_idle())
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 1), NOW + 1)

    def test_analysis_enriches_the_existing_context_map(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()

        self.run_scheduler(scheduler, snapshot)

        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        analysis = stored["analysis"]
        self.assertEqual(
            analysis["current_intent"], "Build conversation drift analysis"
        )
        self.assertEqual(len(analysis["items"]), 10)
        self.assertEqual(analysis["summary"]["relevant_percent"], 100.0)
        self.assertEqual(analysis["summary"]["version"], 3)
        self.assertEqual(analysis["summary"]["method_version"], 3)
        self.assertEqual(analysis["summary"]["session_drift"], analysis["phases"])
        self.assertEqual(
            analysis["summary"]["ai_topics"][0]["label"],
            "Context analysis implementation",
        )
        self.assertEqual(
            analysis["summary"]["topics"],
            analysis["summary"]["technical_categories"],
        )
        self.assertEqual(
            analysis["summary"]["technical_categories"][0],
            {
                "id": "prompts",
                "tokens": 100,
                "relevant_tokens": 100,
                "drifting_tokens": 0,
                "stale_tokens": 0,
                "unanalyzed_tokens": 0,
                "share_percent": 100.0,
            },
        )
        self.assertEqual(analysis["runs"][0]["usage"]["input_tokens"], 100)
        self.assertEqual(analysis["runs"][0]["iteration"], 10)
        self.assertEqual(analysis["summary"]["run_events"][0]["iteration"], 10)
        self.assertNotIn("usage", analysis["summary"]["run_events"][0])
        self.assertNotIn("limit_usage", analysis["runs"][0])
        self.assertEqual(
            snapshot["sessions"][0]["context_map"]["analysis"]["coverage_percent"],
            100.0,
        )
        runner.assert_called_once()

    def test_public_summary_exposes_each_run_without_usage_details(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        analysis = {
            "analyzed_iteration": 10,
            "runs": [
                {
                    "started_at": iso(NOW - 20),
                    "iteration": 5,
                    "usage": {"input_tokens": 10},
                },
                {"started_at": iso(NOW - 10), "usage": {"input_tokens": 20}},
            ],
        }

        runs = context_drift._public_summary(state, analysis)["run_events"]

        self.assertEqual([run["iteration"] for run in runs], [5, 10])
        self.assertTrue(all("usage" not in run for run in runs))

    def test_analysis_receives_the_session_timeline(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())

        prepared = context_drift._payload(state)

        assert prepared is not None
        payload, group_members = prepared
        timeline = payload["conversation_delta"]
        self.assertEqual(len(timeline), 10)
        self.assertEqual(timeline[0]["iteration"], 1)
        self.assertIn("Investigate context drift part 1", timeline[0]["text"])
        self.assertEqual(payload["current_turns"], timeline)
        self.assertEqual(payload["analysis_mode"], "initial")
        self.assertEqual(payload["phase_guidance"]["target_count"], 1)

    def test_incremental_payload_rechecks_only_uncertain_existing_groups(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["analysis"] = {
            "version": 3,
            "method_version": 3,
            "analyzed_iteration": 10,
            "phases": [{"label": "Earlier work"}],
            "items": {
                event["id"]: {
                    "summary": "Earlier context",
                    "ai_topic": "Earlier work",
                    "relevance": 0.5 if index < 4 else 0.9,
                }
                for index, event in enumerate(self.events)
            },
        }

        prepared = context_drift._payload(state)

        assert prepared is not None
        payload, _ = prepared
        self.assertEqual(payload["analysis_mode"], "incremental")
        self.assertEqual(payload["conversation_delta"], [])
        self.assertEqual(payload["prior_session_drift"], [{"label": "Earlier work"}])
        self.assertEqual(len(payload["items"]), 4)

    def test_client_wrappers_are_removed_from_prompts(self) -> None:
        wrapped = (
            "# Files mentioned by the user:\n\n## a.png: /tmp/a.png\n\n"
            '<in-app-browser-context source="ui">\n- url\n</in-app-browser-context>\n\n'
            '## My request:\nremove the C\n\n<image name=[Image #1] path="/tmp/a.png">\n</image>'
        )

        self.assertEqual(context_drift._clean_prompt(wrapped), "remove the C")

    def test_call_targets_cover_paths_commands_patches_and_scripts(self) -> None:
        target = context_drift._call_target
        self.assertEqual(target({"file_path": "/a.py"}), ("file", "/a.py"))
        self.assertEqual(target({"command": "pytest   -q"}), ("command", "pytest -q"))
        self.assertEqual(target("*** Update File: src/x.py\n"), ("file", "src/x.py"))
        self.assertEqual(
            target('await tools.exec_command({cmd:"git status","workdir":"/x"})'),
            ("command", "git status"),
        )

    def test_later_edit_or_rerun_marks_a_group_superseded(self) -> None:
        read = {"id": "r", "iteration": 3, "category": "repository_and_files"}
        edit = {"id": "e", "iteration": 7, "category": "file_changes"}
        run = {"id": "t1", "iteration": 4, "category": "tests_and_build"}
        rerun = {"id": "t2", "iteration": 9, "category": "tests_and_build"}
        events = [read, edit, run, rerun]
        targets = {
            "r": ("file", "/src/a.py"),
            "e": ("file", "/src/a.py"),
            "t1": ("command", "pytest -q"),
            "t2": ("command", "pytest -q"),
        }
        hints = context_drift._superseded_hints

        self.assertEqual(
            hints([read], events, targets), ["a.py was edited again by prompt 7"]
        )
        self.assertEqual(
            hints([run], events, targets), ["the same command ran again by prompt 9"]
        )
        self.assertEqual(hints([rerun], events, targets), [])

    def test_shell_edits_supersede_earlier_reads_of_the_same_file(self) -> None:
        read = {"id": "r", "iteration": 2, "category": "repository_and_files"}
        shell_edit = {"id": "w", "iteration": 6, "category": "repository_and_files"}
        targets = {
            "r": ("command", "sed -n '1,80p' src/konvu_telemetry/context_drift.py"),
            "w": (
                "command",
                "python3 - <<EOF p = Path('src/konvu_telemetry/context_drift.py'); p.write_text(s)",
            ),
        }

        self.assertEqual(
            context_drift._superseded_hints([read], [read, shell_edit], targets),
            ["context_drift.py was edited again by prompt 6"],
        )

    def test_batches_always_include_the_newest_groups(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        template = dict(self.events[0])
        state["iteration"] = 80
        events = [
            {
                **template,
                "id": f"big-{index}",
                "iteration": index,
                "estimated_tokens": 1000,
            }
            for index in range(1, 61)
        ] + [
            {
                **template,
                "id": f"new-{index}",
                "iteration": index,
                "estimated_tokens": 1,
            }
            for index in range(70, 81)
        ]
        state["epochs"][0]["events"] = events

        prepared = context_drift._payload(state)

        assert prepared is not None
        iterations = {item["iteration"] for item in prepared[0]["items"]}
        self.assertTrue(set(range(70, 81)) <= iterations)
        self.assertEqual(
            len(prepared[0]["items"]), context_drift.MAX_INITIAL_ANALYSIS_ITEMS
        )

    def test_excerpt_skips_transcript_bookkeeping(self) -> None:
        record = {
            "parentUuid": "feecb47a-616f-4ea2-b218-fedd7435a972",
            "sessionId": "63f7b321-7880-4d66-825e-b934ee223d03",
            "type": "assistant",
            "message": {
                "model": "claude-opus-5",
                "id": "msg_011CfZyKc5uH1VkAS968ngyu",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Backfill now runs in batches of 45."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "pytest tests"},
                    },
                ],
                "usage": {"service_tier": "standard"},
            },
            "uuid": "3585c708-cb90-462a-a4fa-ff0f82730f6a",
            "timestamp": "2026-10-02T10:00:00Z",
        }

        text = context_drift._strings(record)

        self.assertTrue(text.startswith("Backfill now runs in batches of 45."))
        self.assertIn("pytest tests", text)
        for noise in (
            "msg_011C",
            "claude-opus-5",
            "feecb47a",
            "2026-10-02",
            "standard",
        ):
            self.assertNotIn(noise, text)

    def test_long_timeline_is_sampled_to_the_cap_keeping_both_ends(self) -> None:
        state = {
            "epochs": [
                {
                    "events": [
                        {"id": f"p{index}", "category": "prompts", "iteration": index}
                        for index in range(1, 401)
                    ]
                }
            ]
        }
        with (
            patch.object(context_drift, "_read_record", return_value=None),
            patch.object(context_drift, "_prompt_text", return_value="prompt"),
        ):
            turns, sampled = context_drift._conversation_timeline(state, Path("unused"))

        iterations = [turn["iteration"] for turn in turns]
        self.assertTrue(sampled)
        self.assertEqual(len(turns), context_drift.MAX_TIMELINE_TURNS)
        self.assertEqual(iterations[0], 1)
        self.assertEqual(iterations[-1], 400)
        self.assertEqual(iterations, sorted(set(iterations)))

    def test_long_sessions_request_multiple_drift_phases(self) -> None:
        guidance = context_drift._phase_guidance(378)

        self.assertEqual(guidance["target_count"], 6)
        self.assertEqual(guidance["minimum_count"], 5)
        self.assertEqual(guidance["maximum_count"], 7)

    def test_required_session_infrastructure_cannot_be_marked_stale(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["epochs"][0]["events"] = [
            {**self.events[0], "estimated_tokens": 10},
            {
                "id": "instructions",
                "category": "skills_and_instructions",
                "estimated_tokens": 90,
            },
        ]
        analysis = {
            "items": {
                "event-1": {
                    "ai_topic": "Abandoned work",
                    "relevance": 0.1,
                }
            }
        }

        summary = context_drift._public_summary(state, analysis)

        self.assertEqual(summary["coverage_percent"], 100.0)
        self.assertEqual(summary["relevant_percent"], 90.0)
        self.assertEqual(summary["stale_percent"], 10.0)
        ai_topics = summary["ai_topics"]
        assert isinstance(ai_topics, list)
        self.assertEqual(ai_topics[0]["tokens"], 10)

    def test_sources_from_the_same_turn_and_category_are_grouped(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["epochs"][0]["events"] = [
            {**self.events[0], "id": f"event-{index}", "iteration": 5}
            for index in range(1, 6)
        ]

        prepared = context_drift._payload(state)

        assert prepared is not None
        payload, group_members = prepared
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(len(next(iter(group_members.values()))), 5)

    def test_large_compact_is_cleaned_instead_of_discarded(self) -> None:
        compact = {
            "type": "compacted",
            "payload": {
                "replacement_history": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Keep auth work"}],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Continue the token audit"},
                            {
                                "type": "image",
                                "image_url": "data:image/png;base64," + "x" * 200_000,
                            },
                        ],
                    },
                ],
                "guardian_history": [{"output": "duplicated"}],
            },
        }
        start = self.transcript.stat().st_size
        raw = json.dumps(compact).encode() + b"\n"
        with self.transcript.open("ab") as handle:
            handle.write(raw)
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["epochs"][0]["events"].append(
            {
                "id": "compact-event",
                "category": "previous_compact",
                "label": "Previous compact",
                "iteration": 5,
                "estimated_tokens": 1_000,
                "source_start": start,
                "source_end": start + len(raw),
            }
        )

        prepared = context_drift._payload(state)

        assert prepared is not None
        payload, group_members = prepared
        compact_items = [
            item
            for item in payload["items"]
            if item["technical_category"] == "previous_compact"
        ]
        compact_content = "\n".join(str(item["content"]) for item in compact_items)
        compact_members = [
            member
            for item in compact_items
            for member in group_members[str(item["id"])]
        ]
        self.assertIn("Keep auth work", compact_content)
        self.assertIn("Continue the token audit", compact_content)
        self.assertNotIn("base64", compact_content)
        self.assertEqual(sum(member["tokens"] for member in compact_members), 1_000)
        self.assertEqual(
            {member["source_event_id"] for member in compact_members},
            {"compact-event"},
        )

    def test_partial_valid_result_is_kept_and_remainder_stays_unreviewed(self) -> None:
        def partial(provider: str, payload: dict[str, object]) -> dict[str, object]:
            outcome = self.outcome(provider, payload)
            result = outcome["result"]
            assert isinstance(result, dict)
            items = result["items"]
            assert isinstance(items, list)
            result["items"] = items[:1]
            return outcome

        scheduler = ContextDriftScheduler(Mock(side_effect=partial))
        snapshot = self.snapshot()

        self.run_scheduler(scheduler, snapshot)

        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(stored["analysis"]["state"], "ready")
        self.assertEqual(stored["analysis"]["runs"][0]["status"], "partial")
        self.assertEqual(stored["analysis"]["summary"]["coverage_percent"], 10.0)

    def test_analysis_waits_until_the_context_map_is_stable(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["cursor"] = self.transcript.stat().st_size
        write_private_json(context_map_path("codex", SESSION_ID), state)
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()

        scheduler.refresh(snapshot, self.quotas(), NOW)
        runner.assert_not_called()
        snapshot["sessions"][0]["last_activity_at"] = iso(NOW + 31)
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 31), NOW + 31)
        self.assertTrue(scheduler.wait_for_idle())

        runner.assert_called_once()

    def test_analysis_never_runs_during_an_open_codex_turn(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["cursor"] = self.transcript.stat().st_size
        state["turn_complete"] = False
        write_private_json(context_map_path("codex", SESSION_ID), state)
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()

        scheduler.refresh(snapshot, self.quotas(), NOW)
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 60), NOW + 60)
        runner.assert_not_called()

        state["turn_complete"] = True
        write_private_json(context_map_path("codex", SESSION_ID), state)
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 61), NOW + 61)
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 92), NOW + 92)
        self.assertTrue(scheduler.wait_for_idle())
        runner.assert_called_once()

    def test_old_analysis_version_is_rebuilt(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["analysis"] = {
            "version": 3,
            "method_version": 1,
            "epoch": 0,
            "analyzed_iteration": 0,
            "items": {"event-1": {"ai_topic": "assistant activity metadata"}},
        }
        write_private_json(context_map_path("codex", SESSION_ID), state)
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()

        self.run_scheduler(scheduler, snapshot)

        rebuilt = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(rebuilt["analysis"]["version"], 3)
        self.assertEqual(rebuilt["analysis"]["method_version"], 3)
        self.assertNotIn(
            "assistant activity metadata", rebuilt["analysis"]["ai_topics"]
        )

    def test_pass_records_exact_usage_before_the_next_collector_refresh(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()

        scheduler.refresh(snapshot, self.quotas(20.0), NOW)
        self.assertTrue(scheduler.wait_for_idle())
        usage = json.loads(analysis_usage_path().read_text())
        events = list(usage["events"].values())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["provider"], "codex")
        self.assertEqual(events[0]["parent_session_id"], SESSION_ID)
        self.assertEqual(events[0]["tokens"], 120)
        self.assertGreaterEqual(events[0]["completed_at"], events[0]["started_at"])

    def test_existing_successful_run_self_heals_into_the_usage_ledger(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["analysis"] = {
            "version": 3,
            "state": "ready",
            "epoch": 0,
            "items": {},
            "runs": [
                {
                    "started_at": iso(NOW - 20),
                    "completed_at": iso(NOW - 10),
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                }
            ],
        }
        state["summary"]["analysis"] = {"version": 3, "state": "ready"}
        write_private_json(context_map_path("codex", SESSION_ID), state)
        snapshot = self.snapshot()
        snapshot["sessions"][0]["context_map"]["analysis"] = {
            "version": 3,
            "state": "ready",
        }

        ContextDriftScheduler._refresh_summaries(snapshot)

        usage = json.loads(analysis_usage_path().read_text())
        event = next(iter(usage["events"].values()))
        self.assertEqual(event["tokens"], 120)
        self.assertEqual(event["parent_session_id"], SESSION_ID)

    def test_completed_pass_merges_without_waiting_for_another_limit_reading(
        self,
    ) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()

        scheduler.refresh(snapshot, self.quotas(20.0), NOW)
        self.assertTrue(scheduler.wait_for_idle())
        scheduler.refresh(snapshot, self.quotas(20.0), NOW + 1)
        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(stored["analysis"]["state"], "ready")
        self.assertNotIn("limit_usage", stored["analysis"]["summary"])

    def test_pass_records_mode_breakdown_and_api_price(self) -> None:
        scheduler = ContextDriftScheduler(Mock(side_effect=self.outcome))
        scheduler.refresh(self.snapshot(), self.quotas(), NOW)
        self.assertTrue(scheduler.wait_for_idle())

        event = next(
            iter(json.loads(analysis_usage_path().read_text())["events"].values())
        )
        self.assertEqual(event["usage_mode"], "included")
        self.assertEqual(event["model"], "gpt-6-luna")
        self.assertEqual(event["token_usage"]["input"], 100)
        self.assertEqual(event["token_usage"]["output"], 20)
        self.assertAlmostEqual(event["cost_usd"], 100 * 1e-07 + 20 * 5e-07)

    def test_failed_claude_run_keeps_its_metered_usage(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 1, json.dumps({"usage": {"input_tokens": 7, "output_tokens": 3}}), ""
        )
        with (
            patch(
                "konvu_telemetry.context_drift._trusted_executable",
                return_value="/bin/claude",
            ),
            patch("konvu_telemetry.context_drift._run_cli", return_value=completed),
        ):
            outcome = LocalCliAnalysisRunner()("claude", {"items": []})

        self.assertEqual(outcome["error"], "cli_failed")
        self.assertEqual(outcome["usage"], {"input_tokens": 7, "output_tokens": 3})

    def test_attribution_tokens_do_not_double_count_codex_cache(self) -> None:
        usage = {
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
        }

        self.assertEqual(context_drift._attribution_tokens("codex", usage), 125)

    def test_attribution_tokens_include_claude_cache_buckets(self) -> None:
        usage = {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 40,
        }

        self.assertEqual(context_drift._attribution_tokens("claude", usage), 190)

    def test_analysis_does_not_run_outside_the_plan(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        scheduler.refresh(self.snapshot("exhausted"), self.quotas(), NOW)

        runner.assert_not_called()
        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertNotIn("analysis", stored)

    def test_allow_paid_analyzes_sessions_beyond_the_plan(self) -> None:
        preference = read_preferences()
        write_preferences(
            preference["cadence"],
            preference["custom_rule"],
            preference["jump_percent"],
            context_analysis_allow_paid=True,
        )
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        scheduler.refresh(self.snapshot("beyond_plan"), self.quotas(used=100.0), NOW)
        self.assertTrue(scheduler.wait_for_idle())

        runner.assert_called_once()

    def test_analysis_runs_while_five_percent_of_the_limit_remains(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        scheduler.refresh(self.snapshot(), self.quotas(94), NOW)
        self.assertTrue(scheduler.wait_for_idle())

        runner.assert_called_once()

    def test_analysis_preserves_the_last_five_percent_of_the_usage_limit(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        scheduler.refresh(self.snapshot(), self.quotas(95), NOW)

        runner.assert_not_called()

    def test_analysis_preserves_reserve_in_the_other_subscription_window(self) -> None:
        quotas = self.quotas(20)
        account = quotas["codex"]
        assert isinstance(account, dict)
        windows = account["windows"]
        assert isinstance(windows, list)
        windows.append({"period": "five_hour", "used_percent": 96.0})
        runner = Mock(side_effect=self.outcome)

        ContextDriftScheduler(runner).refresh(self.snapshot(), quotas, NOW)

        runner.assert_not_called()

    def test_analysis_does_not_run_for_an_inactive_session(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()
        snapshot["sessions"][0]["last_activity_at"] = iso(NOW - 301)

        scheduler.refresh(snapshot, self.quotas(), NOW)

        runner.assert_not_called()

    def test_next_pass_waits_for_ten_new_prompts_but_not_for_time(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()
        self.run_scheduler(scheduler, snapshot)
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())

        def add_prompt(iteration: int, now: float) -> None:
            state["iteration"] = iteration
            state["epochs"][0]["events"].append(
                {**self.events[0], "id": f"event-{iteration}", "iteration": iteration}
            )
            write_private_json(context_map_path("codex", SESSION_ID), state)
            snapshot["sessions"][0]["context_map"]["iteration"] = iteration
            snapshot["sessions"][0]["last_activity_at"] = iso(now)
            scheduler.refresh(snapshot, self.quotas(observed_at=now), now)
            self.assertTrue(scheduler.wait_for_idle())

        add_prompt(19, NOW + 2)
        runner.assert_called_once()

        add_prompt(20, NOW + 3)
        self.assertEqual(runner.call_count, 2)
        incremental = runner.call_args.args[1]
        self.assertEqual(incremental["analysis_mode"], "incremental")
        self.assertEqual(
            [turn["iteration"] for turn in incremental["conversation_delta"]],
            [19, 20],
        )
        self.assertEqual(len(incremental["prior_session_drift"]), 1)

    def test_first_pass_waits_for_ten_prompts(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["iteration"] = 9
        write_private_json(context_map_path("codex", SESSION_ID), state)
        runner = Mock(side_effect=self.outcome)

        ContextDriftScheduler(runner).refresh(self.snapshot(), self.quotas(), NOW)

        runner.assert_not_called()

    def test_monthly_credit_meter_does_not_block_codex_subscription_analysis(
        self,
    ) -> None:
        quotas = self.quotas()
        quotas["codex"]["windows"].append({"period": "monthly", "used_percent": 100.0})
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        scheduler.refresh(self.snapshot(), quotas, NOW)
        self.assertTrue(scheduler.wait_for_idle())

        runner.assert_called_once()

    def test_analysis_can_be_disabled_and_the_choice_is_preserved(self) -> None:
        preference = read_preferences()
        write_preferences(
            preference["cadence"],
            preference["custom_rule"],
            preference["jump_percent"],
            context_analysis_enabled=False,
        )
        runner = Mock(side_effect=self.outcome)

        ContextDriftScheduler(runner).refresh(self.snapshot(), self.quotas(), NOW)

        runner.assert_not_called()
        self.assertFalse(read_preferences()["context_analysis_enabled"])

    def test_completed_iteration_is_not_analyzed_twice(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()

        self.run_scheduler(scheduler, snapshot)
        scheduler.refresh(snapshot, self.quotas(), NOW + 2_000)

        runner.assert_called_once()

    def test_long_session_backfills_unclassified_items_in_bounded_batches(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        template = dict(self.events[0])
        state["iteration"] = 65
        state["epochs"][0]["events"] = [
            {**template, "id": f"event-{index}", "iteration": index}
            for index in range(1, 66)
        ]
        write_private_json(context_map_path("codex", SESSION_ID), state)
        snapshot = self.snapshot()
        snapshot["sessions"][0]["context_map"]["iteration"] = 65
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        self.run_scheduler(scheduler, snapshot)
        for offset in (1_201, 2_402, 3_603):
            snapshot["sessions"][0]["last_activity_at"] = iso(NOW + offset)
            scheduler.refresh(
                snapshot, self.quotas(observed_at=NOW + offset), NOW + offset
            )
            self.assertTrue(scheduler.wait_for_idle())
            scheduler.refresh(
                snapshot,
                self.quotas(observed_at=NOW + offset + 1),
                NOW + offset + 1,
            )

        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(len(stored["analysis"]["items"]), 65)
        self.assertEqual(stored["analysis"]["summary"]["coverage_percent"], 100.0)
        self.assertEqual(runner.call_count, 2)

    def write_long_session(self, count: int) -> dict[str, object]:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        template = dict(self.events[0])
        state["iteration"] = count
        state["epochs"][0]["events"] = [
            {**template, "id": f"event-{index}", "iteration": index}
            for index in range(1, count + 1)
        ]
        write_private_json(context_map_path("codex", SESSION_ID), state)
        snapshot = self.snapshot()
        snapshot["sessions"][0]["context_map"]["iteration"] = count
        return snapshot

    def test_backfill_runs_sequentially_without_waiting_for_new_prompts(self) -> None:
        snapshot = self.write_long_session(100)
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        self.run_scheduler(scheduler, snapshot)
        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(stored["analysis"]["summary"]["coverage_percent"], 45.0)
        self.assertEqual(stored["analysis"]["summary"]["backfill_state"], "running")
        for offset in (2, 3):
            self.assertTrue(scheduler.wait_for_idle())
            scheduler.refresh(
                snapshot, self.quotas(observed_at=NOW + offset), NOW + offset
            )
        self.assertTrue(scheduler.wait_for_idle())
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 4), NOW + 4)

        self.assertEqual(runner.call_count, 3)
        modes = [call.args[1]["analysis_mode"] for call in runner.call_args_list]
        self.assertEqual(modes, ["initial", "backfill", "backfill"])
        batches = [
            {item["iteration"] for item in call.args[1]["items"]}
            for call in runner.call_args_list
        ]
        self.assertEqual([len(batch) for batch in batches], [45, 45, 10])
        self.assertFalse(batches[0] & batches[1] or batches[1] & batches[2])
        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(stored["analysis"]["summary"]["coverage_percent"], 100.0)
        self.assertEqual(stored["analysis"]["summary"]["backfill_state"], "complete")

        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 5), NOW + 5)
        self.assertEqual(runner.call_count, 3)

    def test_backfill_resumes_after_a_collector_restart(self) -> None:
        snapshot = self.write_long_session(60)
        self.run_scheduler(
            ContextDriftScheduler(Mock(side_effect=self.outcome)), snapshot
        )
        runner = Mock(side_effect=self.outcome)
        restarted = ContextDriftScheduler(runner)

        restarted.refresh(snapshot, self.quotas(observed_at=NOW + 2), NOW + 2)
        self.assertTrue(restarted.wait_for_idle())

        runner.assert_called_once()
        self.assertEqual(len(runner.call_args.args[1]["items"]), 15)

    def test_backfill_rechecks_the_quota_reserve_before_each_batch(self) -> None:
        snapshot = self.write_long_session(60)
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        scheduler.refresh(snapshot, self.quotas(), NOW)
        self.assertTrue(scheduler.wait_for_idle())

        scheduler.refresh(
            snapshot, self.quotas(used=95.0, observed_at=NOW + 1), NOW + 1
        )
        self.assertTrue(scheduler.wait_for_idle())

        runner.assert_called_once()

    def test_backfill_does_not_loop_on_groups_the_model_omits(self) -> None:
        def drop_last(provider: str, payload: dict[str, object]) -> dict[str, object]:
            outcome = self.outcome(provider, payload)
            result = outcome["result"]
            assert isinstance(result, dict)
            items = result["items"]
            assert isinstance(items, list)
            result["items"] = items[:-1]
            return outcome

        snapshot = self.write_long_session(60)
        runner = Mock(side_effect=drop_last)
        scheduler = ContextDriftScheduler(runner)

        self.run_scheduler(scheduler, snapshot)
        for offset in (2, 3, 4):
            self.assertTrue(scheduler.wait_for_idle())
            scheduler.refresh(
                snapshot, self.quotas(observed_at=NOW + offset), NOW + offset
            )

        self.assertEqual(runner.call_count, 2)
        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(len(stored["analysis"]["items"]), 58)
        self.assertEqual(stored["analysis"]["summary"]["backfill_state"], "complete")

    def test_failed_analysis_uses_exponential_backoff(self) -> None:
        failed = {
            "result": None,
            "model": "unknown",
            "usage": {},
            "duration_seconds": 0.1,
            "error": "timeout",
        }
        runner = Mock(return_value=failed)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()

        scheduler.refresh(snapshot, self.quotas(), NOW)
        self.assertTrue(scheduler.wait_for_idle())
        scheduler.refresh(snapshot, self.quotas(), NOW + 1)
        scheduler.refresh(snapshot, self.quotas(), NOW + 600)
        runner.assert_called_once()
        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(stored["analysis"]["summary"]["state"], "retrying")
        self.assertEqual(stored["analysis"]["last_error"], "timeout")

        snapshot["sessions"][0]["last_activity_at"] = iso(NOW + 1_202)
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 1_202), NOW + 1_202)
        self.assertTrue(scheduler.wait_for_idle())
        self.assertEqual(runner.call_count, 2)

    def test_failed_refresh_keeps_the_previous_analysis_visible(self) -> None:
        scheduler = ContextDriftScheduler(Mock(side_effect=self.outcome))
        snapshot = self.snapshot()
        self.run_scheduler(scheduler, snapshot)
        before = json.loads(context_map_path("codex", SESSION_ID).read_text())
        previous_topics = before["analysis"]["summary"]["ai_topics"]
        failed_job = {
            "run_id": "failed-refresh",
            "provider": "codex",
            "session_id": SESSION_ID,
            "epoch": 0,
            "iteration": 5,
            "group_members": {},
            "started_at": NOW + 2,
            "completed_at": NOW + 3,
            "outcome": {
                "result": None,
                "model": "unknown",
                "usage": {},
                "duration_seconds": 0.1,
                "error": "timeout",
            },
        }

        ContextDriftScheduler._record_failure(snapshot, failed_job, NOW + 3, NOW + 603)

        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        summary = stored["analysis"]["summary"]
        self.assertEqual(summary["state"], "ready")
        self.assertEqual(summary["refresh_state"], "retrying")
        self.assertEqual(summary["ai_topics"], previous_topics)
        self.assertEqual(summary["last_error"], "timeout")

    def test_compaction_epoch_discards_prior_item_classifications(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()
        self.run_scheduler(scheduler, snapshot)
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        compacted = [{**self.events[0], "id": "post-compact", "iteration": 20}]
        state["current_epoch"] = 1
        state["iteration"] = 20
        state["epochs"].append({"index": 1, "events": compacted})
        write_private_json(context_map_path("codex", SESSION_ID), state)
        snapshot["sessions"][0]["context_map"]["iteration"] = 20

        scheduler.refresh(snapshot, self.quotas(), NOW + 2)
        self.assertTrue(scheduler.wait_for_idle())
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 3), NOW + 3)

        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(set(stored["analysis"]["items"]), {"post-compact"})
        self.assertEqual(stored["analysis"]["epoch"], 1)

    def test_claude_runner_is_tool_free_ephemeral_and_budget_limited(self) -> None:
        response = {
            "structured_output": {
                "current_intent": "Test",
                "phases": [
                    {
                        "label": "Test",
                        "summary": "Test",
                        "start_iteration": 1,
                        "end_iteration": None,
                    }
                ],
                "items": [],
            },
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(response), "")
        with (
            patch(
                "konvu_telemetry.context_drift._trusted_executable",
                return_value="/bin/claude",
            ),
            patch(
                "konvu_telemetry.context_drift._run_cli", return_value=completed
            ) as run,
        ):
            outcome = LocalCliAnalysisRunner()("claude", {"items": []})

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(
            run.call_args.args[3],
            {"MAX_THINKING_TOKENS": "0", "DISABLE_PROMPT_CACHING": "1"},
        )
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "0.10")
        self.assertEqual(outcome["usage"]["input_tokens"], 10)

    def test_claude_cli_is_found_outside_the_service_path(self) -> None:
        executable = Path(self.directory.name) / ".local" / "bin" / "claude"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o700)

        with (
            patch("konvu_telemetry.context_drift.shutil.which", return_value=None),
            patch(
                "konvu_telemetry.context_drift.Path.home",
                return_value=Path(self.directory.name),
            ),
        ):
            discovered = context_drift._trusted_executable("claude")

        self.assertEqual(discovered, str(executable.resolve()))

    def test_codex_runner_is_ephemeral_read_only_and_ignores_user_rules(self) -> None:
        def complete(
            command: list[str], _prompt: str, _directory: str
        ) -> subprocess.CompletedProcess[str]:
            result_path = Path(command[command.index("--output-last-message") + 1])
            result_path.write_text(
                json.dumps(
                    {
                        "current_intent": "Test",
                        "phases": [
                            {
                                "label": "Test",
                                "summary": "Test",
                                "start_iteration": 1,
                                "end_iteration": None,
                            }
                        ],
                        "items": [],
                    }
                )
            )
            output = json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 12}}
            )
            return subprocess.CompletedProcess(command, 0, output, "")

        with (
            patch(
                "konvu_telemetry.context_drift._trusted_executable",
                return_value="/bin/codex",
            ),
            patch(
                "konvu_telemetry.context_drift._run_cli", side_effect=complete
            ) as run,
        ):
            outcome = LocalCliAnalysisRunner()("codex", {"items": []})

        command = run.call_args.args[0]
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn('model_reasoning_effort="none"', command)
        self.assertEqual(command.count("--model"), 1)
        self.assertEqual(outcome["usage"]["input_tokens"], 12)

    def test_scheduler_close_terminates_an_active_cli_process(self) -> None:
        worker = Thread(
            target=context_drift._run_cli,
            args=(["/bin/sleep", "60"], "", self.directory.name),
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with context_drift._ACTIVE_PROCESS_LOCK:
                if context_drift._ACTIVE_PROCESSES:
                    break
            time.sleep(0.01)

        ContextDriftScheduler().close()
        worker.join(1)

        self.assertFalse(worker.is_alive())
