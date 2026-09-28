from contextlib import contextmanager
import os
import tempfile
from typing import Iterator
import unittest
from unittest.mock import patch

from konvu_telemetry.display import (
    _current_usage_percent,
    record_usage_shown,
    should_show_usage,
)
from konvu_telemetry.storage import custom_rule_module_path
from konvu_telemetry.preferences import (
    CADENCES,
    DEFAULT_CADENCE,
    custom_rule_prompt,
    read_preferences,
    write_preferences,
)


class PreferencesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.dict(os.environ, {"KONVU_LIVE_USAGE_HOME": self.directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults_apply_when_nothing_is_stored(self) -> None:
        self.assertEqual(read_preferences()["cadence"], DEFAULT_CADENCE)

    def test_an_unknown_cadence_is_rejected_rather_than_coerced(self) -> None:
        with self.assertRaises(ValueError):
            write_preferences("whenever-i-feel-like-it")

    def test_a_custom_cadence_without_a_rule_is_refused(self) -> None:
        # Saving it would store a setting that cannot do anything.
        for empty in ("", "   ", "ab"):
            with self.assertRaises(ValueError):
                write_preferences("custom", empty)
        write_preferences("custom", "only above 80%")
        self.assertEqual(read_preferences()["cadence"], "custom")

    def test_a_custom_rule_is_dropped_when_leaving_the_custom_cadence(self) -> None:
        write_preferences("custom", "only above 80%")
        self.assertEqual(read_preferences()["custom_rule"], "only above 80%")
        write_preferences("never")
        # Keeping the rule would imply it is still in force.
        self.assertEqual(read_preferences()["custom_rule"], "")

    def test_never_suppresses_the_usage_box(self) -> None:
        write_preferences("never")
        self.assertFalse(should_show_usage("claude", "session-a"))

    def test_every_tool_call_needs_a_tool_call(self) -> None:
        write_preferences("every-tool-call")
        self.assertFalse(should_show_usage("claude", "session-a", 0))
        self.assertTrue(should_show_usage("claude", "session-a", 2))

    def test_every_tool_call_reads_the_snapshot_when_no_count_is_passed(self) -> None:
        # The prompt hooks cannot count a turn's tool calls, so the gate looks
        # them up. Without this the cadence would silently never fire there.
        write_preferences("every-tool-call")
        with patch(
            "konvu_telemetry.display.refreshed_session",
            return_value={"last_task_tool_calls": 3},
        ):
            self.assertTrue(should_show_usage("claude", "session-a"))
        with patch(
            "konvu_telemetry.display.refreshed_session",
            return_value={"last_task_tool_calls": 0},
        ):
            self.assertFalse(should_show_usage("claude", "session-a"))

    def test_custom_falls_back_to_showing_when_no_rule_file_exists(self) -> None:
        write_preferences("custom", "only when I ask")
        self.assertTrue(should_show_usage("claude", "session-a"))

    def test_a_user_rule_file_decides_and_survives_upgrades(self) -> None:
        # The rule lives in the user's home directory, not in the package, so
        # replacing the package on upgrade cannot delete it.
        write_preferences("custom", "never on codex")
        path = custom_rule_module_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "def should_show(context):\n    return context['provider'] != 'codex'\n"
        )
        self.assertTrue(should_show_usage("claude", "session-a"))
        self.assertFalse(should_show_usage("codex", "session-a"))

    def test_a_broken_rule_file_shows_rather_than_hides(self) -> None:
        write_preferences("custom", "anything at all")
        path = custom_rule_module_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this is not valid python (((")
        self.assertTrue(should_show_usage("claude", "session-a"))

    def test_usage_jump_shows_once_then_waits_for_the_next_jump(self) -> None:
        write_preferences("usage-jump", jump_percent=2.0)
        with self.binding_window("w1", 10.0):
            self.assertTrue(should_show_usage("claude", "session-a"))
            # Deciding to show is not showing: the baseline only moves once the
            # caller has actually printed the box.
            self.assertTrue(should_show_usage("claude", "session-a"))
            record_usage_shown("claude", "session-a")
            self.assertFalse(should_show_usage("claude", "session-a"))
        with self.binding_window("w1", 11.0):
            self.assertFalse(should_show_usage("claude", "session-a"))
        with self.binding_window("w1", 12.0):
            self.assertTrue(should_show_usage("claude", "session-a"))

    def test_a_new_limit_window_re_arms_the_jump_instead_of_silencing_it(self) -> None:
        write_preferences("usage-jump", jump_percent=2.0)
        with self.binding_window("w1", 90.0):
            self.assertTrue(should_show_usage("claude", "session-a"))
            record_usage_shown("claude", "session-a")
            self.assertFalse(should_show_usage("claude", "session-a"))
        # The window rolls over and the provider restarts near zero. Comparing
        # against the old peak would silence the box for the whole new window.
        with self.binding_window("w2", 2.0):
            self.assertTrue(should_show_usage("claude", "session-a"))
            record_usage_shown("claude", "session-a")
            self.assertFalse(should_show_usage("claude", "session-a"))
        with self.binding_window("w2", 4.5):
            self.assertTrue(should_show_usage("claude", "session-a"))

    def test_the_jump_tracks_the_busiest_limit_bucket(self) -> None:
        """Codex reports one window per bucket; the binding one is what matters."""
        quotas = {
            "codex": {
                "windows": [
                    {
                        "period": "weekly",
                        "limit_id": "codex-mini",
                        "used_percent": 2.0,
                        "resets_at": "w1",
                    },
                    {
                        "period": "weekly",
                        "limit_id": "gpt-5-codex",
                        "used_percent": 45.0,
                        "resets_at": "w1",
                    },
                ]
            }
        }
        reversed_quotas = {
            "codex": {"windows": list(reversed(quotas["codex"]["windows"]))}
        }
        with patch(
            "konvu_telemetry.display.stored_provider_quotas", return_value=quotas
        ):
            self.assertEqual(_current_usage_percent("codex"), 45.0)
        # The same account serialized the other way round must read identically,
        # or a reorder alone would look like a jump.
        with patch(
            "konvu_telemetry.display.stored_provider_quotas",
            return_value=reversed_quotas,
        ):
            self.assertEqual(_current_usage_percent("codex"), 45.0)

    @contextmanager
    def binding_window(self, resets_at: str, used_percent: float) -> Iterator[None]:
        """Pretend the provider reports one limit window at a given usage."""
        quotas = {
            "claude": {
                "windows": [
                    {
                        "period": "five_hour",
                        "limit_id": "default",
                        "used_percent": used_percent,
                        "resets_at": resets_at,
                    }
                ]
            }
        }
        with patch(
            "konvu_telemetry.display.stored_provider_quotas", return_value=quotas
        ):
            yield

    def test_the_agent_prompt_is_actionable_without_reading_the_codebase(self) -> None:
        prompt = custom_rule_prompt("only above 80% weekly")
        self.assertIn("only above 80% weekly", prompt)
        for pointer in (
            "~/.konvu/telemetry/custom_rule.py",
            "def should_show(context: dict) -> bool:",
            "usage_percent",
            "Return True when unsure",
        ):
            self.assertIn(pointer, prompt)
        # Editing the package would be reverted by the next upgrade.
        self.assertIn("do not edit the installed Konvu package", prompt)

    def test_every_cadence_has_a_human_label(self) -> None:
        for key, label in CADENCES.items():
            self.assertTrue(label and label[0].isupper(), key)
