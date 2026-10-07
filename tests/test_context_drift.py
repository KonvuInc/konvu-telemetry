from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import cast
import subprocess
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import Mock, patch

from konvu_telemetry import context_drift
from konvu_telemetry.context_drift import (
    ContextDriftScheduler,
    LocalCliAnalysisRunner,
    PendingJob,
)
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
        # A scheduler closed by an earlier test leaves the cancel flag set.
        context_drift._CANCELLED.clear()
        # HOME too: anything that slips past KONVU_LIVE_USAGE_HOME lands in the temp dir.
        patcher = patch.dict(
            os.environ,
            {"KONVU_LIVE_USAGE_HOME": self.directory.name, "HOME": self.directory.name},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # One runner call per pass keeps call counts meaning passes; batching has its own test.
        batching = patch.object(context_drift, "MAX_BATCH_ITEMS", 10_000)
        batching.start()
        self.addCleanup(batching.stop)
        # Test clocks are not wall clocks; the spending guard has its own test.
        for name, value in (
            ("MIN_SECONDS_BETWEEN_PASSES", 0),
            ("MAX_CALLS_PER_HOUR", 10_000),
        ):
            guard = patch.object(context_drift, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        # The backfill tests below were written around 45 new groups per pass.
        per_pass = patch.object(context_drift, "MAX_INITIAL_ANALYSIS_ITEMS", 45)
        per_pass.start()
        self.addCleanup(per_pass.stop)
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
                "topics": ["Context analysis implementation"],
                "phases": [
                    {
                        "label": "Context drift",
                        "summary": "The session is implementing the classifier.",
                        "start_iteration": 1,
                        "end_iteration": None,
                    }
                ],
                "scores": "\n".join(
                    f"{item['id']}:9:0"
                    + ("" if "card" in item else " | Context drift implementation")
                    for item in items
                    if isinstance(item, dict)
                ),
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
        # A pass the second refresh started must finish inside the test's temp home.
        self.assertTrue(scheduler.wait_for_idle())

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
        self.assertEqual(analysis["summary"]["method_version"], 19)
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
        payload, group_members, _ = prepared
        timeline = payload["conversation_delta"]
        self.assertEqual(len(timeline), 10)
        self.assertEqual(timeline[0]["iteration"], 1)
        self.assertIn("Investigate context drift part 1", timeline[0]["text"])
        self.assertEqual(payload["current_turns"], timeline)
        self.assertEqual(payload["analysis_mode"], "initial")
        self.assertEqual(payload["phase_guidance"]["target_count"], 1)

    def test_every_pass_rerates_all_carded_groups_including_stale_ones(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["iteration"] = 30
        state["analysis"] = {
            "version": 3,
            "method_version": context_drift.ANALYSIS_METHOD_VERSION,
            "analyzed_iteration": 30,
            "phases": [{"label": "Earlier work"}],
            "items": {
                event["id"]: {
                    "ai_topic": "Earlier work",
                    "relevance": 0.1 if index < 4 else 0.9,
                    "card": f"Prompt {index + 1}",
                }
                for index, event in enumerate(self.events)
            },
        }

        prepared = context_drift._payload(state)

        assert prepared is not None
        payload, _, _ = prepared
        self.assertEqual(payload["analysis_mode"], "incremental")
        self.assertEqual(payload["prior_session_drift"], [{"label": "Earlier work"}])
        self.assertEqual(len(payload["items"]), 10)
        self.assertTrue(all(item.get("card") for item in payload["items"]))
        self.assertNotIn("settled", payload)

    def test_item_topics_must_come_from_the_session_topic_list(self) -> None:
        result = {
            "current_intent": "Ship it",
            "topics": ["Quota attribution fixes"],
            "phases": [
                {
                    "label": "Ship",
                    "summary": "s",
                    "start_iteration": 1,
                    "end_iteration": None,
                }
            ],
            "scores": "a1:9:0\nb2:1:-\nc3:5:7",
        }

        self.assertIsNone(context_drift._valid_result(result, {"a1", "b2", "c3"}))

    def test_compact_prompt_is_built_from_keep_and_drop_entries(self) -> None:
        result = {
            "current_intent": "Ship it",
            "compact_keep": ["T1 port.", "  quota  edits ", ""],
            "compact_drop": ["Docker cleanup"],
            "compact_label": "Keep the T1 work",
            "topics": ["T1"],
            "phases": [
                {
                    "label": "Ship",
                    "summary": "s",
                    "start_iteration": 1,
                    "end_iteration": None,
                }
            ],
            "scores": "g1:9:0",
        }

        validated = context_drift._valid_result(result, {"g1"})

        self.assertEqual(
            validated["compact_prompt"],
            "Preserve: T1 port; quota edits. Drop: Docker cleanup.",
        )
        result["compact_keep"] = []
        self.assertEqual(
            context_drift._valid_result(result, {"g1"})["compact_prompt"], ""
        )

    def test_rerating_keeps_cards_and_sends_only_cards(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()
        self.run_scheduler(scheduler, snapshot)
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        for index in range(11, 21):
            state["epochs"][0]["events"].append(
                {**self.events[0], "id": f"event-{index}", "iteration": index}
            )
        state["iteration"] = 20
        write_private_json(context_map_path("codex", SESSION_ID), state)
        snapshot["sessions"][0]["context_map"]["iteration"] = 20

        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 2), NOW + 2)
        self.assertTrue(scheduler.wait_for_idle())
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 3), NOW + 3)

        second = runner.call_args.args[1]["items"]
        self.assertEqual(sum(1 for item in second if "card" in item), 10)
        self.assertEqual(sum(1 for item in second if "card" not in item), 10)
        items = json.loads(context_map_path("codex", SESSION_ID).read_text())[
            "analysis"
        ]["items"]
        self.assertTrue(all(item.get("card") for item in items.values()))

    def test_compact_command_is_built_from_the_prompt(self) -> None:
        analysis = {"compact_prompt": "Keep the T1 port."}

        high = context_drift._compact_advice(
            analysis, {"relevant": 70, "drifting": 10, "stale": 20}, 100
        )

        self.assertNotIn("compact_recommended", high)
        self.assertEqual(high["compact_command"], "/compact Keep the T1 port.")
        self.assertNotIn("compact_label", high)
        labelled = context_drift._compact_advice(
            {**analysis, "compact_label": "Keep the T1 work"},
            {"relevant": 70, "drifting": 10, "stale": 20},
            100,
        )
        self.assertEqual(labelled["compact_label"], "Keep the T1 work")
        self.assertEqual(
            context_drift._compact_advice(
                {}, {"relevant": 0, "drifting": 50, "stale": 50}, 100
            ),
            {},
        )

    def test_compact_scores_are_parsed_and_model_cards_ignored(self) -> None:
        result = {
            "current_intent": "Ship it",
            "topics": ["Quota attribution fixes"],
            "phases": [
                {
                    "label": "Ship",
                    "summary": "s",
                    "start_iteration": 1,
                    "end_iteration": None,
                }
            ],
            "scores": "g1:9:0 | Read of quota_attribution.py\ng2:1:- | Bare ls output\ng3:0.5:7\ng9:3:0",
        }

        valid = context_drift._valid_result(result, {"g1", "g2", "g3"})

        assert valid is not None
        self.assertEqual(
            valid["items"],
            {
                "g1": {"ai_topic": "Quota attribution fixes", "relevance": 0.9},
                "g2": {"ai_topic": "", "relevance": 0.1},
            },
        )
        # A decimal score is ambiguous, so g3 stays unrated and the run is partial.
        self.assertFalse(valid["complete"])

    def test_latest_prompt_context_is_never_rated_stale(self) -> None:
        def all_stale(provider: str, payload: dict[str, object]) -> dict[str, object]:
            outcome = self.outcome(provider, payload)
            result = outcome["result"]
            assert isinstance(result, dict)
            result["scores"] = str(result["scores"]).replace(":9:", ":0:")
            return outcome

        scheduler = ContextDriftScheduler(Mock(side_effect=all_stale))
        self.run_scheduler(scheduler, self.snapshot())

        items = json.loads(context_map_path("codex", SESSION_ID).read_text())[
            "analysis"
        ]["items"]
        self.assertEqual(items["event-10"]["relevance"], 0.8)
        self.assertEqual(items["event-9"]["relevance"], 0.0)

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
        steps = context_drift._later_steps(events, targets)

        def hints(group: list[dict[str, object]]) -> list[str]:
            return context_drift._superseded_hints(group, steps, targets)

        self.assertEqual(hints([read]), ["a.py was edited again by prompt 7"])
        self.assertEqual(hints([run]), ["the same command ran again by prompt 9"])
        self.assertEqual(hints([rerun]), [])

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
            context_drift._superseded_hints(
                [read], context_drift._later_steps([read, shell_edit], targets), targets
            ),
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
        tokens = {row["label"]: row["tokens"] for row in ai_topics}
        self.assertEqual(tokens, {"Session setup": 90, "Abandoned work": 10})

    def test_sources_from_the_same_turn_and_category_are_grouped(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["epochs"][0]["events"] = [
            {**self.events[0], "id": f"event-{index}", "iteration": 5}
            for index in range(1, 6)
        ]

        prepared = context_drift._payload(state)

        assert prepared is not None
        payload, group_members, _ = prepared
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
        payload, group_members, _ = prepared
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
            result["scores"] = str(result["scores"]).splitlines()[0]
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
        self.assertEqual(rebuilt["analysis"]["method_version"], 19)
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

        scheduler.refresh(self.snapshot("exhausted"), self.quotas(used=100.0), NOW)
        self.assertTrue(scheduler.wait_for_idle())

        runner.assert_called_once()

    def test_allow_paid_still_keeps_the_plan_reserve_for_plan_sessions(self) -> None:
        preference = read_preferences()
        write_preferences(
            preference["cadence"],
            preference["custom_rule"],
            preference["jump_percent"],
            context_analysis_allow_paid=True,
        )
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        scheduler.refresh(self.snapshot(), self.quotas(95), NOW)

        runner.assert_not_called()

    def test_allow_paid_never_runs_a_session_whose_plan_is_unknown(self) -> None:
        preference = read_preferences()
        write_preferences(
            preference["cadence"],
            preference["custom_rule"],
            preference["jump_percent"],
            context_analysis_allow_paid=True,
        )
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        scheduler.refresh(
            self.snapshot("unknown"),
            {"claude": {"status": "unavailable", "windows": []}},
            NOW,
        )

        runner.assert_not_called()

    def test_analysis_runs_while_ten_percent_of_the_limit_remains(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        scheduler.refresh(self.snapshot(), self.quotas(89), NOW)
        self.assertTrue(scheduler.wait_for_idle())

        runner.assert_called_once()

    def test_analysis_preserves_the_last_ten_percent_of_the_usage_limit(self) -> None:
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

    def test_older_method_ratings_are_rebuilt_without_waiting(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["analysis"] = {"version": 3, "method_version": 3, "analyzed_iteration": 9}

        self.assertEqual(ContextDriftScheduler._due(state), "delta")

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

    def test_a_cancelled_pass_starts_no_further_batches(self) -> None:
        payload = {"items": [{"id": "g1"}, {"id": "g2"}, {"id": "g3"}]}
        runner = Mock(side_effect=self.outcome)

        def lead_then_cancel(provider: str, batch: dict[str, object]) -> object:
            context_drift.cancel_active_analysis()
            return runner(provider, batch)

        with patch.object(context_drift, "MAX_BATCH_ITEMS", 1):
            outcome = context_drift._run_batched(lead_then_cancel, "claude", payload)

        runner.assert_called_once()
        self.assertEqual(outcome["batch_errors"], ["cancelled", "cancelled"])

    def test_turning_analysis_off_cancels_the_running_pass(self) -> None:
        scheduler = ContextDriftScheduler(Mock(side_effect=self.outcome))
        scheduler._pending = cast(PendingJob, {})
        preference = read_preferences()
        write_preferences(
            preference["cadence"],
            preference["custom_rule"],
            preference["jump_percent"],
            context_analysis_enabled=False,
        )

        with patch.object(context_drift, "cancel_active_analysis") as cancel:
            scheduler.refresh(self.snapshot(), self.quotas(), NOW)

        cancel.assert_called_once()

    def test_an_unsaved_result_backs_off_like_a_failure(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        snapshot = self.snapshot()
        scheduler.refresh(snapshot, self.quotas(), NOW)
        self.assertTrue(scheduler.wait_for_idle())

        with (
            patch.object(
                context_drift, "write_private_json_if_changed", side_effect=OSError
            ),
            self.assertLogs("konvu_telemetry.context_drift", level="ERROR"),
        ):
            scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 60), NOW + 60)
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 120), NOW + 120)
        self.assertTrue(scheduler.wait_for_idle())

        runner.assert_called_once()

    def test_a_stored_backoff_survives_a_collector_restart(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["analysis"] = {"retry_at": iso(NOW + 600)}
        write_private_json(context_map_path("codex", SESSION_ID), state)
        runner = Mock(side_effect=self.outcome)

        ContextDriftScheduler(runner).refresh(self.snapshot(), self.quotas(), NOW)

        runner.assert_not_called()

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
            {item["iteration"] for item in call.args[1]["items"] if "card" not in item}
            for call in runner.call_args_list
        ]
        self.assertEqual([len(batch) for batch in batches], [45, 45, 10])
        self.assertFalse(batches[0] & batches[1] or batches[1] & batches[2])
        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(stored["analysis"]["summary"]["coverage_percent"], 100.0)
        self.assertEqual(stored["analysis"]["summary"]["backfill_state"], "complete")

        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 5), NOW + 5)
        self.assertEqual(runner.call_count, 3)

    def test_groups_after_the_last_pass_do_not_keep_backfill_running(self) -> None:
        old = ((3, "repository_and_files", "a.py"), [{"id": "old"}])
        # Leftovers of the analyzed prompt itself still belong to the backlog.
        same = ((9, "repository_and_files", "c.py"), [{"id": "same"}])
        new = ((10, "repository_and_files", "b.py"), [{"id": "new"}])

        backlog = context_drift._backlog_groups(
            [old, same, new], {"items": {}, "analyzed_iteration": 9}
        )

        self.assertEqual(backlog, [old, same])

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
        new_items = [i for i in runner.call_args.args[1]["items"] if "card" not in i]
        self.assertEqual(len(new_items), 15)

    def test_backfill_rechecks_the_quota_reserve_before_each_pass(self) -> None:
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
            result["scores"] = "\n".join(str(result["scores"]).splitlines()[:-1])
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
        # The group pass one omitted is retried and rated in pass two; the one pass two
        # omits waits, never looping.
        self.assertEqual(len(stored["analysis"]["items"]), 59)
        self.assertEqual(stored["analysis"]["summary"]["backfill_state"], "complete")

    def test_backfill_passes_send_only_new_groups(self) -> None:
        snapshot = self.write_long_session(100)
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        self.run_scheduler(scheduler, snapshot)
        self.assertTrue(scheduler.wait_for_idle())
        scheduler.refresh(snapshot, self.quotas(observed_at=NOW + 2), NOW + 2)
        self.assertTrue(scheduler.wait_for_idle())

        second = runner.call_args_list[1].args[1]
        self.assertEqual(second["analysis_mode"], "backfill")
        self.assertFalse([item for item in second["items"] if "card" in item])

    def test_context_arriving_during_a_pass_waits_for_new_prompts(self) -> None:
        def add_late_event(
            provider: str, payload: dict[str, object]
        ) -> dict[str, object]:
            if runner.call_count == 1:
                state = json.loads(context_map_path("codex", SESSION_ID).read_text())
                template = dict(state["epochs"][0]["events"][0])
                state["epochs"][0]["events"].append(
                    {**template, "id": "late", "iteration": 5, "tool": "late"}
                )
                write_private_json(context_map_path("codex", SESSION_ID), state)
            return self.outcome(provider, payload)

        snapshot = self.write_long_session(20)
        runner = Mock(side_effect=add_late_event)
        scheduler = ContextDriftScheduler(runner)

        self.run_scheduler(scheduler, snapshot)
        for offset in (2, 3):
            self.assertTrue(scheduler.wait_for_idle())
            scheduler.refresh(
                snapshot, self.quotas(observed_at=NOW + offset), NOW + offset
            )

        runner.assert_called_once()

    def test_reply_excerpt_names_each_call_instead_of_the_tool_alone(self) -> None:
        claude = {
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "hidden", "signature": "x"},
                    {"type": "text", "text": "Wiring the routes."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {
                            "command": "git push\nmore",
                            "description": "Push the branch",
                        },
                    },
                ]
            }
        }
        codex = {
            "payload": {
                "type": "function_call",
                "name": "exec",
                "arguments": '{"cmd": "pytest -q"}',
            }
        }

        self.assertEqual(
            context_drift._reply_text(claude),
            "Wiring the routes.\nBash: Push the branch",
        )
        self.assertEqual(context_drift._reply_text(codex), "exec: pytest -q")

    def test_large_passes_are_rated_in_batches_sharing_one_topic_list(self) -> None:
        payload = {"items": [{"id": f"g{index}"} for index in range(1, 6)]}
        calls: list[dict[str, object]] = []

        def runner(provider: str, batch: dict[str, object]) -> dict[str, object]:
            calls.append(batch)
            ids = [item["id"] for item in batch["items"]]
            topics = ["Newest work"] if "g5" in ids else ["Old work", "Newest work"]
            return {
                "result": {
                    "current_intent": "x",
                    "topics": topics,
                    "phases": [],
                    "scores": "\n".join(
                        f"{i}:7:{len(topics) - 1}" if i == "g1" else f"{i}:2:0"
                        for i in ids
                    ),
                },
                "model": "m",
                "usage": {"input_tokens": 10},
                "duration_seconds": 1.0,
                "error": None,
            }

        with patch.object(context_drift, "MAX_BATCH_ITEMS", 2):
            outcome = context_drift._run_batched(runner, "claude", payload)

        self.assertEqual(len(calls), 3)
        self.assertEqual([item["id"] for item in calls[0]["items"]], ["g4", "g5"])
        self.assertEqual(calls[1]["existing_ai_topics"], ["Newest work"])
        result = outcome["result"]
        self.assertEqual(result["topics"], ["Newest work", "Old work"])
        lines = dict(line.split(":", 1) for line in result["scores"].splitlines())
        self.assertEqual(lines["g1"], "7:0")
        self.assertEqual(lines["g2"], "2:1")
        self.assertEqual(outcome["usage"]["input_tokens"], 30)

    def test_a_failed_older_batch_keeps_the_other_batches(self) -> None:
        payload = {"items": [{"id": f"g{index}"} for index in range(1, 5)]}

        def runner(provider: str, batch: dict[str, object]) -> dict[str, object]:
            ids = [item["id"] for item in batch["items"]]
            if "g1" in ids:
                return {
                    "result": None,
                    "model": "m",
                    "usage": {},
                    "duration_seconds": 1.0,
                    "error": "timeout",
                }
            # A stray id from another batch must not be taken.
            lines = [f"{i}:8:0" for i in ids] + ["g1:0:0"]
            return {
                "result": {
                    "current_intent": "x",
                    "topics": ["Work"],
                    "phases": [],
                    "scores": "\n".join(lines),
                },
                "model": "m",
                "usage": {},
                "duration_seconds": 1.0,
                "error": None,
            }

        with patch.object(context_drift, "MAX_BATCH_ITEMS", 2):
            outcome = context_drift._run_batched(runner, "claude", payload)

        self.assertIsNone(outcome["error"])
        lines = dict(
            line.split(":", 1) for line in outcome["result"]["scores"].splitlines()
        )
        # The stray g1 line came from a batch that was not sent g1, so it is dropped.
        self.assertEqual(sorted(lines), ["g3", "g4"])

    def test_spending_guard_spaces_passes_and_caps_them_per_hour(self) -> None:
        def state(*ages: float) -> dict[str, object]:
            return {
                "analysis": {"runs": [{"completed_at": iso(NOW - age)} for age in ages]}
            }

        with (
            patch.object(context_drift, "MIN_SECONDS_BETWEEN_PASSES", 30),
            patch.object(context_drift, "MAX_CALLS_PER_HOUR", 3),
        ):
            self.assertTrue(context_drift._over_budget(state(10), NOW))
            self.assertFalse(context_drift._over_budget(state(60), NOW))
            self.assertTrue(context_drift._over_budget(state(60, 600, 1200), NOW))
            self.assertFalse(context_drift._over_budget(state(60, 600, 4000), NOW))

    def test_spending_cap_counts_model_calls_not_passes(self) -> None:
        state = {"analysis": {"runs": [{"completed_at": iso(NOW - 600), "calls": 4}]}}

        with patch.object(context_drift, "MAX_CALLS_PER_HOUR", 6):
            self.assertFalse(context_drift._over_budget(state, NOW, 2))
            self.assertTrue(context_drift._over_budget(state, NOW, 3))

    def test_scores_must_be_whole_numbers(self) -> None:
        self.assertEqual(context_drift._fraction("7"), 0.7)
        self.assertIsNone(context_drift._fraction("1.0"))
        self.assertIsNone(context_drift._fraction("11"))

    def test_large_turns_split_so_every_event_is_in_the_excerpt(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        template = dict(state["epochs"][0]["events"][0])
        state["epochs"][0]["events"] = [
            {
                **template,
                "id": f"e{index}",
                "iteration": 1,
                "tool": "t",
                "estimated_tokens": 10,
            }
            for index in range(20)
        ]

        groups = context_drift._analysis_groups(state, Path(state["source_path"]))

        self.assertTrue(
            all(len(events) <= context_drift.MAX_GROUP_EVENTS for _, events in groups)
        )

    def test_every_member_of_a_large_group_is_in_its_excerpt(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        template = dict(state["epochs"][0]["events"][0])
        state["epochs"][0]["events"] = [
            {
                **template,
                "id": f"e{index}",
                "iteration": 1,
                "tool": "t",
                "label": "A very long label that would crowd the excerpt",
                "estimated_tokens": 10,
                "analysis_content": f"member-{index:02d} " + "x" * 200,
            }
            for index in range(20)
        ]
        state["analysis"] = {}

        prepared = context_drift._payload(state)

        assert prepared is not None
        content = " ".join(
            str(item.get("content", "")) for item in prepared[0]["items"]
        )
        self.assertEqual(
            [index for index in range(20) if f"member-{index:02d}" not in content], []
        )

    def test_old_method_ratings_are_hidden_and_rebuilt_at_once(self) -> None:
        old = {
            "version": context_drift.ANALYSIS_VERSION,
            "method_version": 1,
            "analyzed_iteration": 3,
        }
        state = {"iteration": 4, "current_epoch": 0, "analysis": old}

        self.assertEqual(ContextDriftScheduler._due(state), "delta")
        summary = context_drift._rebuilding_summary(
            {"relevant_percent": 80, "current_intent": "x"}
        )
        self.assertEqual(summary["state"], "rebuilding")
        self.assertNotIn("relevant_percent", summary)

    def test_the_overall_call_cap_survives_a_restart(self) -> None:
        context_drift.record_analysis_usage(
            "claude",
            "s",
            "run-1",
            NOW - 120,
            NOW - 60,
            100,
            model="m",
            cost_usd=0.01,
            calls=4,
        )

        self.assertEqual(len(context_drift._recent_call_starts(NOW)), 4)

    def test_a_claude_compaction_is_rated_from_its_summary(self) -> None:
        boundary = {
            "type": "system",
            "subtype": "compact_boundary",
            "compactMetadata": {"preTokens": 90_000, "postTokens": 2_000},
        }
        summary = {
            "type": "user",
            "isCompactSummary": True,
            "message": {
                "role": "user",
                "content": "1. Goal:\n   Keep auth work\n\n2. Pending:\n   Finish the token audit",
            },
        }
        start = self.transcript.stat().st_size
        raw = json.dumps(boundary).encode() + b"\n"
        with self.transcript.open("ab") as handle:
            handle.write(raw + json.dumps(summary).encode() + b"\n")
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["epochs"][0]["events"].append(
            {
                "id": "compact-event",
                "category": "previous_compact",
                "label": "Previous compact",
                "iteration": 5,
                "estimated_tokens": 2_000,
                "source_start": start,
                "source_end": start + len(raw),
            }
        )

        prepared = context_drift._payload(state)

        assert prepared is not None
        payload, group_members, _ = prepared
        compact_items = [
            item
            for item in payload["items"]
            if item["technical_category"] == "previous_compact"
        ]
        self.assertEqual(len(compact_items), 2)
        self.assertIn("Keep auth work", str(compact_items[0]["content"]))
        self.assertIn("Finish the token audit", str(compact_items[1]["content"]))
        self.assertEqual(
            sum(
                member["tokens"]
                for item in compact_items
                for member in group_members[str(item["id"])]
            ),
            2_000,
        )

    def test_an_unreadable_event_is_shown_to_the_model_before_it_is_rated(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["epochs"][0]["events"] = [
            {**self.events[0], "id": "readable", "iteration": 5},
            {
                **self.events[0],
                "id": "blank",
                "iteration": 5,
                "analysis_content": "",
                "estimated_tokens": 700,
            },
        ]

        prepared = context_drift._payload(state)

        assert prepared is not None
        payload, group_members, _ = prepared
        self.assertEqual(len(payload["items"]), 1)
        self.assertIn(
            "no readable text, about 700 tokens", payload["items"][0]["content"]
        )
        self.assertEqual(
            {member["id"] for member in group_members["g1"]}, {"readable", "blank"}
        )

    def test_compact_entries_keep_only_plain_naming_characters(self) -> None:
        entries = context_drift._compact_entries(
            ["PR 91 review; rm -rf ~ `x` $(y) <z>", "quota_attribution.py edits"], 5
        )

        self.assertEqual(
            entries, ["PR 91 review rm -rf x (y) z", "quota_attribution.py edits"]
        )

    def test_codex_summary_includes_compact_focus_for_a_current_analysis(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        state["epochs"][0]["events"][0]["estimated_tokens"] = 100
        analysis = {
            "epoch": state["current_epoch"],
            "compact_prompt": "Preserve: the open dashboard work. Drop: finished setup.",
            "items": {
                "event-1": {"ai_topic": "Finished setup", "relevance": 0.1},
            },
        }

        summary = context_drift._public_summary(state, analysis)

        self.assertEqual(summary["compact_prompt"], analysis["compact_prompt"])
        self.assertEqual(
            summary["compact_command"], "/compact " + analysis["compact_prompt"]
        )
        self.assertEqual(summary["state"], "ready")

    def test_a_compaction_clears_old_advice_and_makes_the_session_due(self) -> None:
        state = {
            "iteration": 12,
            "current_epoch": 2,
            "analysis": {
                "version": context_drift.ANALYSIS_VERSION,
                "method_version": context_drift.ANALYSIS_METHOD_VERSION,
                "epoch": 1,
                "analyzed_iteration": 11,
                "current_intent": "Old goal",
                "compact_prompt": "Preserve: old work.",
                "items": {},
            },
        }

        self.assertEqual(ContextDriftScheduler._due(state), "delta")
        summary = context_drift._public_summary(state, state["analysis"])
        self.assertEqual(summary["current_intent"], "")
        self.assertNotIn("compact_command", summary)

    def test_topics_only_an_older_batch_names_are_kept(self) -> None:
        payload = {"items": [{"id": "g1"}, {"id": "g2"}]}

        def runner(provider: str, batch: dict[str, object]) -> dict[str, object]:
            ids = [item["id"] for item in batch["items"]]
            topics = ["Lead work"] if "g2" in ids else ["lead  WORK", "Side task"]
            lines = "g2:8:0" if "g2" in ids else "g1:3:1"
            return {
                "result": {
                    "current_intent": "x",
                    "topics": topics,
                    "phases": [],
                    "scores": lines,
                },
                "model": "m",
                "usage": {},
                "duration_seconds": 1.0,
                "error": None,
            }

        with patch.object(context_drift, "MAX_BATCH_ITEMS", 1):
            outcome = context_drift._run_batched(runner, "claude", payload)

        result = outcome["result"]
        self.assertEqual(result["topics"], ["Lead work", "Side task"])
        self.assertIn("g1:3:1", result["scores"].splitlines())

    def test_stored_excerpts_mask_credentials_but_keep_paths(self) -> None:
        redact = context_drift._redact

        self.assertEqual(
            redact("OPENAI=sk-abcdefghijklmnopqrstuvwx"), "OPENAI=[redacted]"
        )
        self.assertEqual(redact("password = hunter2 ok"), "[redacted] ok")
        path = "cd /Users/ag/Desktop/code/konvu-telemetry-context-drift && ls"
        self.assertEqual(redact(path), path)
        for leaked in (
            "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            '{"password": "hunter2"}',
            "DATABASE_URL=postgres://admin:S3cretPass@db:5432/x",
            "Authorization: Bearer abcDEF123456789xyz",
        ):
            self.assertNotIn("hunter2", redact(leaked))
            self.assertNotIn("S3cretPass", redact(leaked))
            self.assertNotIn("wJalrXUtnFEMI", redact(leaked))
            self.assertNotIn("abcDEF123456789xyz", redact(leaked))
        for ordinary in (
            "input_tokens: 812, output_tokens: 90",
            "max_tokens=4096",
            "the tokenizer: splits words",
            "fn(token: str) -> None",
            "secret_scanning: enabled",
            "credentials: see docs",
        ):
            self.assertEqual(redact(ordinary), ordinary)

    def test_a_deeply_nested_record_is_read_without_recursing_forever(self) -> None:
        nested: object = "deep text"
        for _ in range(5_000):
            nested = {"content": nested}

        self.assertEqual(
            context_drift._strings({"top": "visible", "rest": nested}), "visible"
        )

    def test_scores_glued_without_newlines_still_parse(self) -> None:
        matches = context_drift._SCORE_ENTRY.findall("g1:9:0g2:5:1")

        self.assertEqual(matches, [("g1", "9", "0"), ("g2", "5", "1")])

    def test_starting_context_counts_as_session_setup(self) -> None:
        self.assertIn("starting_context", context_drift.FIXED_RELEVANT_CATEGORIES)

    def test_large_turn_groups_are_split_into_chunks(self) -> None:
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        template = dict(state["epochs"][0]["events"][0])
        state["epochs"][0]["events"] = [
            {
                **template,
                "id": f"e{index}",
                "iteration": 1,
                "tool": "assistant",
                "estimated_tokens": 3_000,
            }
            for index in range(7)
        ]

        groups = context_drift._analysis_groups(state, Path(state["source_path"]))

        self.assertEqual([len(events) for _, events in groups], [2, 2, 2, 1])
        self.assertEqual(
            [key[2] for key, _ in groups],
            ["assistant", "assistant #2", "assistant #3", "assistant #4"],
        )

    def test_backfill_ends_when_only_unreadable_groups_remain(self) -> None:
        snapshot = self.write_long_session(60)
        state = json.loads(context_map_path("codex", SESSION_ID).read_text())
        # The oldest, smallest group has no text; it is sent as a placeholder and rated once.
        state["epochs"][0]["events"][0]["analysis_content"] = ""
        state["epochs"][0]["events"][0]["estimated_tokens"] = 1
        write_private_json(context_map_path("codex", SESSION_ID), state)
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)

        self.run_scheduler(scheduler, snapshot)
        for offset in (2, 3, 4):
            self.assertTrue(scheduler.wait_for_idle())
            scheduler.refresh(
                snapshot, self.quotas(observed_at=NOW + offset), NOW + offset
            )

        self.assertEqual(runner.call_count, 2)
        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertEqual(stored["analysis"]["summary"]["backfill_state"], "complete")

    def test_a_cancelled_pass_leaves_no_failure_or_backoff(self) -> None:
        def cancelled(provider: str, payload: dict[str, object]) -> dict[str, object]:
            context_drift.cancel_active_analysis()
            return {
                "result": None,
                "model": "unknown",
                "usage": {},
                "duration_seconds": 0.1,
                "error": "cli_failed",
            }

        scheduler = ContextDriftScheduler(Mock(side_effect=cancelled))
        scheduler.refresh(self.snapshot(), self.quotas(), NOW)
        self.assertTrue(scheduler.wait_for_idle())

        scheduler.refresh(self.snapshot(), self.quotas(observed_at=NOW + 1), NOW + 1)

        stored = json.loads(context_map_path("codex", SESSION_ID).read_text())
        self.assertNotIn("retry_at", stored.get("analysis") or {})
        self.assertEqual(scheduler._failure_counts, {})

    def test_the_overall_hourly_call_cap_holds_across_sessions(self) -> None:
        runner = Mock(side_effect=self.outcome)
        scheduler = ContextDriftScheduler(runner)
        scheduler._call_starts = [NOW - 60] * context_drift.MAX_GLOBAL_CALLS_PER_HOUR

        scheduler.refresh(self.snapshot(), self.quotas(), NOW)

        runner.assert_not_called()

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
        self.assertEqual(
            command[command.index("--model") + 1],
            context_drift.CLAUDE_ANALYSIS_MODEL,
        )
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--strict-mcp-config", command)
        # The user's CLAUDE.md, rules and hooks must not reach the rater.
        self.assertIn("--setting-sources=", command)
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
        # Untrusted transcript text gets no shell, files, web or apps.
        disabled = {
            command[index + 1]
            for index, flag in enumerate(command)
            if flag == "--disable"
        }
        self.assertTrue(
            {"shell_tool", "unified_exec", "apps", "computer_use"} <= disabled
        )
        self.assertIn('web_search="disabled"', command)
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
