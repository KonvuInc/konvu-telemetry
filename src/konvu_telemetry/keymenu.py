"""A small arrow-key picker for the terminal, with no third-party dependencies.

One row may carry an editable field, so a choice that needs a value can be typed
without a second prompt. Everything degrades to a plain numbered list when there
is no terminal to read keys from.
"""

from __future__ import annotations

from contextlib import contextmanager
import sys
import termios
import tty
from typing import Iterator, Sequence

UP = "up"
DOWN = "down"
ENTER = "enter"
CANCEL = "cancel"
BACKSPACE = "backspace"

BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"

_ESCAPE_SEQUENCES = {"[A": UP, "OA": UP, "[B": DOWN, "OB": DOWN}
# Ctrl-C and Ctrl-D leave the picker the same way Escape does.
_CONTROL_KEYS = {"\r": ENTER, "\n": ENTER, "\x03": CANCEL, "\x04": CANCEL}


def interactive() -> bool:
    """Whether keys can be read one at a time from a real terminal."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except ValueError:
        return False


@contextmanager
def raw_terminal() -> Iterator[None]:
    """Hold the terminal in raw mode for a whole session, not one key at a time.

    Restoring between keys would let anything typed in the gap echo and queue up
    on the cooked line, which is exactly what a picker must not do.
    """
    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def read_key() -> str:
    """Return one keypress as a name, or the literal character that was typed.

    Must be called inside raw_terminal().
    """
    first = sys.stdin.read(1)
    if first == "\x1b":
        # An arrow arrives as an escape sequence; a bare Escape does not.
        sequence = sys.stdin.read(2)
        return _ESCAPE_SEQUENCES.get(sequence, CANCEL)
    if first in ("\x7f", "\b"):
        return BACKSPACE
    return _CONTROL_KEYS.get(first, first)


class Picker:
    """Draw a list of labels, move a cursor through it, and edit one field."""

    def __init__(
        self,
        labels: Sequence[str],
        *,
        selected: int = 0,
        editable_row: int | None = None,
        field: str = "",
        placeholder: str = "",
        current_row: int | None = None,
        field_limit: int = 2000,
    ) -> None:
        self.labels = list(labels)
        self.selected = min(max(selected, 0), len(self.labels) - 1)
        self.editable_row = editable_row
        self.field = field
        self.placeholder = placeholder
        self.current_row = current_row
        self.field_limit = field_limit
        self._drawn = 0

    def _row_text(self, index: int) -> str:
        focused = index == self.selected
        pointer = "❯" if focused else " "
        box = "[✓]" if index == self.current_row else "[ ]"
        label = self.labels[index]
        if focused:
            label = f"{BOLD}{label}{RESET}"
        row = f"  {pointer}  {box}  {label}"
        if index != self.editable_row:
            return row
        if self.field:
            value = self.field
        elif focused:
            value = f"{DIM}{self.placeholder}{RESET}"
        else:
            value = ""
        caret = "▏" if focused else ""
        return f"{row}{'   ' if value or caret else ''}{value}{caret}"

    def draw(self) -> None:
        if self._drawn:
            # Redraw in place so the list does not scroll away on every keypress.
            sys.stdout.write(f"\x1b[{self._drawn}A")
        lines = 0
        for index in range(len(self.labels)):
            sys.stdout.write("\x1b[2K" + self._row_text(index) + "\r\n")
            lines += 1
            # A blank line between rows; the list is short, so it can afford the air.
            if index < len(self.labels) - 1:
                sys.stdout.write("\x1b[2K\r\n")
                lines += 1
        sys.stdout.flush()
        self._drawn = lines

    def apply(self, key: str) -> bool:
        """Handle one key. Returns False once the picker should stop drawing."""
        if key in (ENTER, CANCEL):
            return False
        if key == UP:
            self.selected = (self.selected - 1) % len(self.labels)
            return True
        if key == DOWN:
            self.selected = (self.selected + 1) % len(self.labels)
            return True
        if self.selected != self.editable_row:
            return True
        if key == BACKSPACE:
            self.field = self.field[:-1]
        elif len(key) == 1 and key.isprintable() and len(self.field) < self.field_limit:
            self.field += key
        return True

    def run(self) -> tuple[int, str] | None:
        """Draw until Enter or cancel. Returns the row and field, or None."""
        sys.stdout.write("\x1b[?25l")
        try:
            with raw_terminal():
                while True:
                    self.draw()
                    key = read_key()
                    if key == ENTER:
                        return self.selected, self.field
                    if key == CANCEL:
                        return None
                    self.apply(key)
        finally:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()
