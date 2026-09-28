"""User preferences for how often the Konvu usage box is shown."""

from __future__ import annotations

import json
from typing import TypedDict

from .storage import preferences_path, write_private_json

# How often the Konvu usage box is drawn inside a turn, in Codex CLI, Codex
# desktop and Claude desktop. The Claude CLI status line is separate: it is
# ambient and always current, so it is not gated here.
CADENCES: dict[str, str] = {
    "every-prompt": "After every prompt",
    "every-tool-call": "After every tool call",
    "usage-jump": "Only when usage jumps",
    "never": "Never",
    "custom": "Custom rule",
}
# After a tool call is the useful default: a turn that called tools is the one
# where usage actually moved.
DEFAULT_CADENCE = "every-tool-call"
# A provider reports whole percentages, so one point is the smallest real move.
DEFAULT_JUMP_PERCENT = 1.0
# A custom cadence with no rule saves a setting that cannot do anything, so it
# is refused rather than accepted and quietly ignored.
MIN_CUSTOM_RULE_LENGTH = 3


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
    if cadence == "custom" and len(custom_rule.strip()) < MIN_CUSTOM_RULE_LENGTH:
        raise ValueError("A custom cadence needs a rule to follow")
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
    """A prompt a coding agent can act on without reading the codebase first.

    It points at a file in the user's home directory rather than at the
    installed package: editing the package would work until the next upgrade
    replaced it, silently reverting the rule.
    """
    return f"""Write a Konvu telemetry rule that decides when the usage box appears
in the Claude and Codex CLI hooks. The rule I want:

    "{rule.strip() or "describe your rule here"}"

Create this file — do not edit the installed Konvu package, because upgrading
replaces it and would silently delete your rule:

    ~/.konvu/telemetry/custom_rule.py

It must define one function:

    def should_show(context: dict) -> bool:
        ...

Return True to show the usage box for this turn, False to stay quiet.

What context contains
  - provider:      "claude" or "codex"
  - session_id:    the current session's id
  - tool_calls:    tool calls in the most recent turn (int)
  - rule:          the rule text above, as I typed it
  - usage_percent: percent of the active limit window used account-wide, or
                   None if it is not known yet. The window is the 5-hour one
                   for Claude and the weekly one for Codex.
  - session:       the full session record from the collector, or None. It
                   carries context_tokens, context_window_tokens, task_count,
                   total_cost_usd, projected_next_10_tasks_usd and
                   quota_attribution.

Rules to respect
  - Return True when unsure. Silently suppressing output is the worse failure,
    and any exception is already treated as True.
  - Keep it quick: this runs on every turn, before my prompt is answered.
  - It has no access to Konvu internals beyond context, so do not import from
    konvu_telemetry.

To check it, set the cadence to custom and run a turn:

    konvu-telemetry cadence custom --rule "{rule.strip() or "..."}"
"""
