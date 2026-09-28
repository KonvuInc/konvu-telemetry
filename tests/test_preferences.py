from contextlib import contextmanager, redirect_stdout
from io import StringIO
import json
import os
import tempfile
from typing import Iterator
import unittest
from unittest.mock import patch

from konvu_telemetry.display import (
    _binding_quota_window,
    _current_usage_percent,
    record_usage_shown,
    should_show_usage,
)
from konvu_telemetry.cli import cadence_main
from konvu_telemetry.storage import custom_rule_module_path, preferences_path
from konvu_telemetry.preferences import (
    CADENCES,
    ensure_preferences_file,
    DEFAULT_CADENCE,
    DEFAULT_JUMP_PERCENT,
    MAX_CUSTOM_RULE_LENGTH,
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
    def binding_window(
        self,
        resets_at: str,
        used_percent: float,
        provider: str = "claude",
        period: str = "five_hour",
    ) -> Iterator[None]:
        """Pretend the provider reports one limit window at a given usage."""
        quotas = {
            provider: {
                "windows": [
                    {
                        "period": period,
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

    def test_a_rolled_over_window_re_arms_even_when_usage_climbed(self) -> None:
        """Guards the window identity on its own, without help from the drop check."""
        write_preferences("usage-jump", jump_percent=50.0)
        with self.binding_window("w1", 10.0):
            self.assertTrue(should_show_usage("claude", "s"))
            record_usage_shown("claude", "s")
        # Higher than the baseline but under the threshold, so only the changed
        # window identity can explain a box here.
        with self.binding_window("w2", 20.0):
            self.assertTrue(should_show_usage("claude", "s"))

    def test_a_figure_that_went_backwards_re_arms_within_one_window(self) -> None:
        """Guards the drop check on its own, with the window identity held fixed."""
        write_preferences("usage-jump", jump_percent=50.0)
        with self.binding_window("w1", 80.0):
            self.assertTrue(should_show_usage("claude", "s"))
            record_usage_shown("claude", "s")
            self.assertFalse(should_show_usage("claude", "s"))
        with self.binding_window("w1", 79.0):
            self.assertTrue(should_show_usage("claude", "s"))

    def test_equal_buckets_pick_the_same_one_whatever_the_order(self) -> None:
        """Two buckets at the same percent must not look like a jump on reorder."""
        windows = [
            {
                "period": "weekly",
                "limit_id": "a",
                "used_percent": 40.0,
                "resets_at": "r",
            },
            {
                "period": "weekly",
                "limit_id": "b",
                "used_percent": 40.0,
                "resets_at": "r",
            },
        ]
        keys = []
        for ordering in (windows, list(reversed(windows))):
            with patch(
                "konvu_telemetry.display.stored_provider_quotas",
                return_value={"codex": {"windows": ordering}},
            ):
                keys.append(_binding_quota_window("codex"))
        self.assertEqual(keys[0], keys[1])

    def test_an_unavailable_provider_shows_once_rather_than_every_turn(self) -> None:
        write_preferences("usage-jump", jump_percent=1.0)
        with patch(
            "konvu_telemetry.display.stored_provider_quotas",
            return_value={"codex": {"status": "unavailable", "windows": []}},
        ):
            self.assertTrue(should_show_usage("codex", "s"))
            record_usage_shown("codex", "s")
            self.assertFalse(should_show_usage("codex", "s"))
        # Once the provider answers again the figure is real, so the box returns.
        with self.binding_window("w1", 5.0, provider="codex", period="weekly"):
            self.assertTrue(should_show_usage("codex", "s"))

    def test_an_over_long_rule_is_refused_rather_than_stored_in_part(self) -> None:
        with self.assertRaises(ValueError):
            write_preferences("custom", "x" * (MAX_CUSTOM_RULE_LENGTH + 1))
        self.assertEqual(read_preferences()["cadence"], DEFAULT_CADENCE)

    def test_a_hand_edited_file_cannot_turn_custom_into_always_show(self) -> None:
        for stored in ({"cadence": "custom", "custom_rule": ""}, {"cadence": "custom"}):
            preferences_path().write_text(json.dumps(stored))
            self.assertEqual(read_preferences()["cadence"], DEFAULT_CADENCE)
        # A jump threshold that is not a positive number is dropped, not honoured.
        for bad in ("5", 0, -1, True):
            preferences_path().write_text(
                json.dumps({"cadence": "usage-jump", "jump_percent": bad})
            )
            self.assertEqual(read_preferences()["jump_percent"], DEFAULT_JUMP_PERCENT)

    def test_a_file_that_is_not_utf8_still_honours_the_chosen_cadence(self) -> None:
        """A latin-1 byte from an editor must not raise into a hook or the API."""
        preferences_path().write_bytes(
            b'{"cadence": "never", "custom_rule": "\xe0 80%"}'
        )
        self.assertEqual(read_preferences()["cadence"], "never")
        self.assertFalse(should_show_usage("claude", "s", tool_calls=5))
        # Unparseable content is a different case: there is no choice left to honour.
        preferences_path().write_bytes(b'{"cadence": "never"')
        self.assertEqual(read_preferences()["cadence"], DEFAULT_CADENCE)

    def test_the_menu_keeps_the_threshold_passed_on_the_command_line(self) -> None:
        """A flag given without a cadence must reach the write, not be dropped."""
        with patch("builtins.input", return_value="3"), redirect_stdout(StringIO()):
            cadence_main(["--jump-percent", "25"])
        stored = read_preferences()
        self.assertEqual(stored["cadence"], "usage-jump")
        self.assertEqual(stored["jump_percent"], 25.0)

    def test_the_menu_reports_an_empty_custom_rule_instead_of_crashing(self) -> None:
        output = StringIO()
        with patch("builtins.input", side_effect=["5", "  "]), redirect_stdout(output):
            cadence_main([])
        self.assertIn("Nothing changed", output.getvalue())
        self.assertEqual(read_preferences()["cadence"], DEFAULT_CADENCE)

    def test_an_unwritable_home_is_reported_rather_than_raised(self) -> None:
        output = StringIO()
        with (
            patch(
                "konvu_telemetry.cli.write_preferences",
                side_effect=OSError(13, "Permission denied"),
            ),
            redirect_stdout(output),
        ):
            with self.assertRaises(SystemExit) as exit_code:
                cadence_main(["never"])
        self.assertEqual(exit_code.exception.code, 2)
        self.assertIn("Could not save your choice", output.getvalue())

    def test_setup_writes_the_default_so_nothing_is_only_implicit(self) -> None:
        """The panel must never show a choice selected that was never stored."""
        self.assertFalse(preferences_path().exists())
        self.assertEqual(ensure_preferences_file()["cadence"], DEFAULT_CADENCE)
        self.assertEqual(
            json.loads(preferences_path().read_text())["cadence"], DEFAULT_CADENCE
        )

    def test_setup_never_overwrites_a_choice_already_made(self) -> None:
        write_preferences("never")
        ensure_preferences_file()
        self.assertEqual(read_preferences()["cadence"], "never")

    def test_an_unwritable_home_still_reports_the_cadence_in_force(self) -> None:
        with patch(
            "konvu_telemetry.preferences.write_private_json",
            side_effect=OSError(13, "Permission denied"),
        ):
            self.assertEqual(ensure_preferences_file()["cadence"], DEFAULT_CADENCE)

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
