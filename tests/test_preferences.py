import os
import tempfile
import unittest
from unittest.mock import patch

from konvu_telemetry.display import should_show_usage
from konvu_telemetry.preferences import (
    CADENCES,
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
        self.assertEqual(read_preferences()["cadence"], "every-prompt")

    def test_an_unknown_cadence_is_rejected_rather_than_coerced(self) -> None:
        with self.assertRaises(ValueError):
            write_preferences("whenever-i-feel-like-it")

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

    def test_custom_falls_back_to_showing_rather_than_hiding(self) -> None:
        # The rule is not implemented until the user's agent writes it, so the
        # safe behaviour is the default, not silence.
        write_preferences("custom", "only when I ask")
        self.assertTrue(should_show_usage("claude", "session-a"))

    def test_usage_jump_shows_once_then_waits_for_the_next_jump(self) -> None:
        write_preferences("usage-jump", jump_percent=2.0)
        with patch("konvu_telemetry.display._current_usage_percent", return_value=10.0):
            self.assertTrue(should_show_usage("claude", "session-a"))
            self.assertFalse(should_show_usage("claude", "session-a"))
        with patch("konvu_telemetry.display._current_usage_percent", return_value=12.0):
            self.assertTrue(should_show_usage("claude", "session-a"))

    def test_the_agent_prompt_names_the_rule_and_where_to_change_it(self) -> None:
        prompt = custom_rule_prompt("only above 80% weekly")
        self.assertIn("only above 80% weekly", prompt)
        self.assertIn("should_show_usage()", prompt)

    def test_every_cadence_has_a_human_label(self) -> None:
        for key, label in CADENCES.items():
            self.assertTrue(label and label[0].isupper(), key)
