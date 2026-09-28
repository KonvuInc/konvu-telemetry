import unittest
from unittest.mock import patch

from konvu_telemetry import keymenu
from konvu_telemetry.keymenu import Picker

LABELS = ["After every prompt", "After every tool call", "Never", "Custom rule"]
CUSTOM_ROW = 3


def picker(**overrides: object) -> Picker:
    options: dict[str, object] = {
        "selected": 0,
        "editable_row": CUSTOM_ROW,
        "current_row": 1,
    }
    options.update(overrides)
    return Picker(LABELS, **options)  # type: ignore[arg-type]


class PickerTests(unittest.TestCase):
    def test_arrows_move_the_cursor_and_wrap_at_both_ends(self) -> None:
        menu = picker()
        menu.apply(keymenu.DOWN)
        self.assertEqual(menu.selected, 1)
        menu.apply(keymenu.UP)
        menu.apply(keymenu.UP)
        self.assertEqual(menu.selected, len(LABELS) - 1)
        menu.apply(keymenu.DOWN)
        self.assertEqual(menu.selected, 0)

    def test_typing_only_edits_the_field_while_its_row_is_focused(self) -> None:
        menu = picker(selected=0)
        for key in "abc":
            menu.apply(key)
        self.assertEqual(menu.field, "", "typing on a plain row must not edit the rule")
        menu.selected = CUSTOM_ROW
        for key in "80%":
            menu.apply(key)
        self.assertEqual(menu.field, "80%")
        menu.apply(keymenu.BACKSPACE)
        self.assertEqual(menu.field, "80")

    def test_the_field_stops_at_its_limit(self) -> None:
        menu = picker(selected=CUSTOM_ROW, field_limit=3)
        for key in "abcdef":
            menu.apply(key)
        self.assertEqual(menu.field, "abc")

    def test_a_row_shows_which_choice_is_current_and_which_is_focused(self) -> None:
        menu = picker(selected=0)
        self.assertIn("❯", menu._row_text(0))
        self.assertIn("[ ]", menu._row_text(0))
        # The stored choice keeps its tick wherever the cursor happens to be.
        self.assertIn("[✓]", menu._row_text(1))
        self.assertNotIn("❯", menu._row_text(1))

    def test_the_placeholder_shows_only_on_the_focused_empty_field(self) -> None:
        menu = picker(selected=CUSTOM_ROW, placeholder="describe your rule")
        self.assertIn("describe your rule", menu._row_text(CUSTOM_ROW))
        menu.selected = 0
        self.assertNotIn("describe your rule", menu._row_text(CUSTOM_ROW))
        menu.field = "over 80%"
        self.assertIn("over 80%", menu._row_text(CUSTOM_ROW))

    def test_enter_and_cancel_stop_the_loop(self) -> None:
        menu = picker()
        self.assertFalse(menu.apply(keymenu.ENTER))
        self.assertFalse(menu.apply(keymenu.CANCEL))
        self.assertTrue(menu.apply(keymenu.DOWN))

    def test_rows_are_separated_by_a_blank_line_when_drawn(self) -> None:
        menu = picker()
        written: list[str] = []
        with patch("sys.stdout") as stdout:
            stdout.write.side_effect = written.append
            menu.draw()
        self.assertEqual(menu._drawn, len(LABELS) * 2 - 1)


if __name__ == "__main__":
    unittest.main()
