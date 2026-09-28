"""User preferences for how often usage is shown in the CLI hooks."""

from __future__ import annotations

import json
from typing import TypedDict

from .storage import preferences_path, write_private_json

# How often the usage box is injected into a CLI turn. The status line is
# separate: it is ambient and always current, so it is not gated here.
CADENCES: dict[str, str] = {
    "every-prompt": "After every prompt",
    "every-tool-call": "After every tool call",
    "usage-jump": "Only when usage jumps",
    "never": "Never",
    "custom": "Custom rule",
}
DEFAULT_CADENCE = "every-prompt"
# A provider reports whole percentages, so one point is the smallest real move.
DEFAULT_JUMP_PERCENT = 1.0


class Preferences(TypedDict):
    cadence: str
    custom_rule: str
    jump_percent: float


def _default() -> Preferences:
    return {
        "cadence": DEFAULT_CADENCE,
        "custom_rule": "",
        "jump_percent": DEFAULT_JUMP_PERCENT,
    }


def read_preferences() -> Preferences:
    """Read preferences, falling back to defaults for anything unreadable."""
    try:
        raw = json.loads(preferences_path().read_text())
    except (OSError, json.JSONDecodeError):
        return _default()
    if not isinstance(raw, dict):
        return _default()
    value = _default()
    cadence = raw.get("cadence")
    if isinstance(cadence, str) and cadence in CADENCES:
        value["cadence"] = cadence
    rule = raw.get("custom_rule")
    if isinstance(rule, str):
        value["custom_rule"] = rule[:2000]
    jump = raw.get("jump_percent")
    if isinstance(jump, (int, float)) and not isinstance(jump, bool) and jump > 0:
        value["jump_percent"] = float(jump)
    return value


def write_preferences(
    cadence: str, custom_rule: str = "", jump_percent: float | None = None
) -> Preferences:
    """Store a cadence choice. An unknown cadence is rejected, not coerced."""
    if cadence not in CADENCES:
        raise ValueError("Unknown cadence: " + cadence)
    current = read_preferences()
    value: Preferences = {
        "cadence": cadence,
        # A custom rule is only meaningful for the custom cadence; keeping a
        # stale one around would misrepresent what is in force.
        "custom_rule": custom_rule[:2000] if cadence == "custom" else "",
        "jump_percent": float(jump_percent)
        if isinstance(jump_percent, (int, float)) and jump_percent > 0
        else current["jump_percent"],
    }
    write_private_json(preferences_path(), value)
    return value


def custom_rule_prompt(rule: str) -> str:
    """The text a user hands to a coding agent to implement their own rule."""
    return (
        "In the Konvu telemetry CLI, change when the usage box is shown in the "
        "CLI hooks so that it follows this rule:\n\n"
        f"    {rule.strip() or '(describe your rule here)'}\n\n"
        "The gate lives in konvu_telemetry/display.py, in should_show_usage(). "
        "It reads preferences from konvu_telemetry/preferences.py and is called "
        "by the prompt hooks before they print. Keep the existing cadences "
        "working, and add tests covering the new rule."
    )
