"""User preferences for how often the Konvu usage box is shown."""

from __future__ import annotations

import json
from typing import Literal, TypedDict

from .storage import (
    preferences_path,
    preferences_write_lock,
    write_private_json,
)

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
# Long enough for any rule a person writes by hand. An over-long one is refused
# rather than cut down, so the stored rule is always the rule that was typed.
MAX_CUSTOM_RULE_LENGTH = 2000


class Preferences(TypedDict):
    cadence: str
    custom_rule: str
    jump_percent: float
    context_analysis_enabled: bool
    context_analysis_consent: Literal["unset", "enabled", "disabled"]
    context_analysis_allow_paid: bool


def _default() -> Preferences:
    return {
        "cadence": DEFAULT_CADENCE,
        "custom_rule": "",
        "jump_percent": DEFAULT_JUMP_PERCENT,
        # On by default within the plan; beyond-plan use stays opt-in.
        "context_analysis_enabled": True,
        "context_analysis_consent": "unset",
        "context_analysis_allow_paid": False,
    }


def read_preferences() -> Preferences:
    """Read preferences, falling back to defaults for anything unreadable.

    A hand-edited file can hold anything, so every field is re-validated with the
    same rules the write path enforces, and a decoding failure yields the default
    rather than escaping into a hook or an HTTP handler.
    """
    try:
        # errors="replace" keeps a stray non-UTF-8 byte from raising out of here.
        text = preferences_path().read_bytes().decode("utf-8", errors="replace")
        raw = json.loads(text)
    except (OSError, ValueError):
        return _default()
    if not isinstance(raw, dict):
        return _default()
    value = _default()
    rule = raw.get("custom_rule")
    if isinstance(rule, str) and len(rule) <= MAX_CUSTOM_RULE_LENGTH:
        value["custom_rule"] = rule
    cadence = raw.get("cadence")
    if isinstance(cadence, str) and cadence in CADENCES:
        value["cadence"] = cadence
    # A stored custom cadence whose rule did not survive validation would mean
    # "always show", the one outcome write_preferences refuses to save.
    if value["cadence"] == "custom" and not _rule_is_usable(value["custom_rule"]):
        value["cadence"] = DEFAULT_CADENCE
    if value["cadence"] != "custom":
        value["custom_rule"] = ""
    jump = raw.get("jump_percent")
    if isinstance(jump, (int, float)) and not isinstance(jump, bool) and jump > 0:
        value["jump_percent"] = float(jump)
    consent = raw.get("context_analysis_consent")
    if consent in {"enabled", "disabled"}:
        value["context_analysis_consent"] = consent
        value["context_analysis_enabled"] = consent == "enabled"
    if raw.get("context_analysis_allow_paid") is True:
        value["context_analysis_allow_paid"] = True
    return value


def _rule_is_usable(rule: str) -> bool:
    return (
        MIN_CUSTOM_RULE_LENGTH <= len(rule.strip())
        and len(rule) <= MAX_CUSTOM_RULE_LENGTH
    )


def ensure_preferences_file() -> Preferences:
    """Write the effective preferences to disk if nothing is stored yet.

    Until a file exists the cadence is an implicit default: the settings panel
    shows a choice selected that was never actually saved, so the only way to be
    sure of what is in force is to press Save. Materialising it at setup makes
    the displayed choice and the stored one the same thing from the first run.
    """
    current = read_preferences()
    if preferences_path().exists():
        return current
    try:
        write_private_json(preferences_path(), current)
    except OSError:
        # A preference that cannot be written still applies; it is the default.
        pass
    return current


def write_preferences(
    cadence: str,
    custom_rule: str = "",
    jump_percent: float | None = None,
    context_analysis_enabled: bool | None = None,
    context_analysis_allow_paid: bool | None = None,
) -> Preferences:
    """Store a cadence choice. An unknown cadence is rejected, not coerced."""
    if cadence not in CADENCES:
        raise ValueError("Unknown cadence: " + cadence)
    if cadence == "custom":
        if len(custom_rule.strip()) < MIN_CUSTOM_RULE_LENGTH:
            raise ValueError("A custom cadence needs a rule to follow")
        if len(custom_rule) > MAX_CUSTOM_RULE_LENGTH:
            # Storing a cut-down rule would report success for a rule nobody wrote.
            raise ValueError(
                f"A custom rule must be {MAX_CUSTOM_RULE_LENGTH} characters or fewer"
            )
    # Two writers (the command and the dashboard) share this file, so the
    # read-modify-write that carries jump_percent forward runs under a lock.
    with preferences_write_lock():
        current = read_preferences()
        value: Preferences = {
            "cadence": cadence,
            # A custom rule is only meaningful for the custom cadence; keeping a
            # stale one around would misrepresent what is in force.
            "custom_rule": custom_rule if cadence == "custom" else "",
            "jump_percent": float(jump_percent)
            if isinstance(jump_percent, (int, float)) and jump_percent > 0
            else current["jump_percent"],
            "context_analysis_enabled": (
                context_analysis_enabled
                if isinstance(context_analysis_enabled, bool)
                else current["context_analysis_enabled"]
            ),
            "context_analysis_consent": (
                "enabled" if context_analysis_enabled else "disabled"
            )
            if isinstance(context_analysis_enabled, bool)
            else current["context_analysis_consent"],
            "context_analysis_allow_paid": (
                context_analysis_allow_paid
                if isinstance(context_analysis_allow_paid, bool)
                else current["context_analysis_allow_paid"]
            ),
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
