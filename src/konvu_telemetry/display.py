"""Claude status-line and provider hook output."""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from .config import (
    ACTIVITY_FRESHNESS_SECONDS,
    ALERT_QUOTA_5H_PERCENT,
    ALERT_QUOTA_WEEKLY_PERCENT,
    ALLOWED_PROVIDERS,
    CLAUDE_DESKTOP_ENTRYPOINT,
    CODEX_DISPLAY_COST_THRESHOLD_USD,
    CODEX_DISPLAY_MIN_TASKS,
)
from .parsers import (
    claude_hook_transcript,
    codex_client_in_file,
    codex_hook_transcript,
    codex_turn_tool_calls,
)
from .storage import (
    claude_quota_path,
    codex_display_state_path,
    session_path,
    snapshot_path,
    valid_session_id,
    write_private_json,
)

# Desktop clients hide hook system messages, so the box has to ride in as model context instead.
PROMPT_BOX_INSTRUCTION = (
    "Append the block below verbatim as the very last thing in your reply, "
    "after all other text including any closing line, with no commentary."
)


def record_claude_quotas(payload: dict[str, object], session_id: str) -> None:
    """Persist fresh Claude quota windows reported to the status-line hook."""
    if not valid_session_id(session_id):
        return
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return
    windows: list[dict[str, object]] = []
    for name, minutes in (("five_hour", 5 * 60), ("seven_day", 7 * 24 * 60)):
        raw_window = rate_limits.get(name)
        if not isinstance(raw_window, dict):
            continue
        value = next(
            (
                raw_window.get(key)
                for key in ("utilization", "used_percentage", "used_pct")
                if isinstance(raw_window.get(key), (int, float))
            ),
            None,
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        used_percent = value * 100 if 0 <= value <= 1 else value
        if not math.isfinite(used_percent) or used_percent < 0:
            continue
        used_percent = min(100.0, used_percent)
        resets_at = next(
            (
                raw_window.get(key)
                for key in ("resets_at", "reset_at")
                if isinstance(raw_window.get(key), str)
            ),
            None,
        )
        windows.append(
            {
                "limit_id": "default",
                "session_id": session_id,
                "window_minutes": minutes,
                "used_percent": used_percent,
                "remaining_percent": 100 - used_percent,
                "resets_at": resets_at,
            }
        )
    if windows:
        write_private_json(
            claude_quota_path(),
            {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": "claude_statusline",
                "windows": windows,
            },
        )


def money(value: object) -> str:
    return (
        f"${float(value):.1f}"
        if isinstance(value, (int, float))
        else "price unavailable"
    )


def tokens(value: object) -> str:
    if not isinstance(value, int):
        return "0"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def relative_age(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return "unknown"
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 5:
        return "now"
    if seconds < 10:
        return "5s ago"
    if seconds < 20:
        return "10s ago"
    if seconds < 50:
        return "20s ago"
    if seconds < 60:
        return "50s ago"
    if seconds < 120:
        return "1m ago"
    if seconds < 300:
        return "2m ago"
    if seconds < 600:
        return "5m ago"
    if seconds < 3600:
        return "10m ago"
    return f"{seconds // 3600}h ago"


def baseline_text(session: dict[str, object]) -> str:
    comparison = session.get("baseline")
    if not isinstance(comparison, dict):
        return ""
    emoji = (
        comparison.get("emoji") if isinstance(comparison.get("emoji"), str) else "⚪"
    )
    overhead = comparison.get("cost_overhead_percent")
    if not isinstance(overhead, int):
        overhead = comparison.get("token_overhead_percent")
    iterations = comparison.get("iterations")
    if not isinstance(overhead, int) or not isinstance(iterations, int):
        return "⚪ baseline unavailable"
    direction = "below" if overhead < 0 else "over"
    return f"{emoji} {abs(overhead)}% {direction} your median"


def quota_usage_text(payload: dict[str, object]) -> str:
    """Render Claude's provider-reported five-hour and weekly quota usage."""
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return ""

    def percentage(window_name: str) -> int | None:
        window = rate_limits.get(window_name)
        if not isinstance(window, dict):
            return None
        value = next(
            (
                window.get(key)
                for key in ("utilization", "used_percentage", "used_pct")
                if isinstance(window.get(key), (int, float))
            ),
            None,
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        percentage = value * 100 if 0 <= value <= 1 else value
        return round(min(100, percentage)) if math.isfinite(percentage) else None

    five_hour = percentage("five_hour")
    weekly = percentage("seven_day")
    parts: list[str] = []
    if five_hour is not None:
        hot = "🔥 " if five_hour >= ALERT_QUOTA_5H_PERCENT else ""
        parts.append(f"{hot}⏳ {five_hour}% 5-hour limit")
    if weekly is not None:
        hot = "🔥 " if weekly >= ALERT_QUOTA_WEEKLY_PERCENT else ""
        parts.append(f"{hot}📅 {weekly}% weekly limit")
    return " · ".join(parts)


def recorded_quota_usage_text(provider: str) -> str:
    """Read the collector's latest quota windows for a provider hook."""
    try:
        snapshot = json.loads(snapshot_path().read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    accounts = snapshot.get("account_quotas") if isinstance(snapshot, dict) else None
    account = accounts.get(provider) if isinstance(accounts, dict) else None
    windows = account.get("windows") if isinstance(account, dict) else None
    parts: list[str] = []
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict):
            continue
        minutes = window.get("window_minutes")
        used = window.get("used_percent")
        if not isinstance(minutes, (int, float)) or not isinstance(used, (int, float)):
            continue
        label = (
            "5-hour"
            if minutes == 300
            else "weekly"
            if minutes == 10_080
            else f"{round(minutes / 60)}-hour"
        )
        parts.append(f"{round(min(100, max(0, used)))}% {label} limit")
    return " · ".join(parts)


def subagent_usage_text(session: dict[str, object]) -> str:
    """Summarise the live and total child-agent footprint in one short line."""
    total = session.get("subagent_total")
    live = session.get("active_subagents")
    handed = session.get("subagent_entry_context_tokens")
    context = session.get("context_tokens")
    spend = session.get("subagent_cost_usd")
    if not isinstance(total, int) or total == 0:
        return ""
    shared_percentage = None
    if isinstance(handed, int) and isinstance(context, int) and context > 0:
        shared_percentage = round(handed / (context * total) * 100)
    shared_text = (
        f"{min(100, shared_percentage)}% context shared"
        if shared_percentage is not None
        else tokens(handed)
    )
    return f"🤖 {live or 0} live / {total} total · {shared_text} · {money(spend)} spent"


def context_usage_text(session: dict[str, object]) -> str:
    """Render context as a percentage when the provider exposes its window size."""
    context = session.get("context_tokens")
    window = session.get("context_window_tokens")
    if isinstance(context, int) and isinstance(window, int) and window > 0:
        return f"{round(context / window * 100)}% context"
    return f"{tokens(context)} context"


def statusline() -> None:
    """Render only the local metrics that belong to the current Claude session."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("Konvu live usage: waiting for Claude session data")
        return
    if not isinstance(payload, dict):
        print("Konvu live usage: waiting for Claude session data")
        return
    session_id = payload.get("session_id")
    if (
        not isinstance(session_id, str)
        or claude_hook_transcript(payload, session_id) is None
    ):
        print("Konvu live usage: collector starting")
        return
    record_claude_quotas(payload, session_id)
    snapshot = refreshed_session("claude", session_id)
    if snapshot is None:
        print("Konvu live usage: collector starting")
        return
    try:
        session = snapshot
    except (OSError, json.JSONDecodeError):
        session = None
    if not isinstance(session, dict):
        sessions = snapshot.get("sessions")
        session = (
            next(
                (
                    item
                    for item in sessions
                    if isinstance(item, dict) and item.get("id") == session_id
                ),
                None,
            )
            if isinstance(sessions, list)
            else None
        )
    if session is None:
        print("Konvu live usage: waiting for this session's local file")
        return
    context = payload.get("context_window")
    used_percentage = (
        context.get("used_percentage") if isinstance(context, dict) else None
    )
    context_text = (
        f"{float(used_percentage):.0f}%"
        if isinstance(used_percentage, (int, float))
        else "waiting for first response"
    )
    quota_text = quota_usage_text(payload)
    complete = session.get("cost_status") == "complete"
    forecast = session.get("projected_next_10_tasks_usd")
    forecast_text = (
        f"{money(forecast)} for the next 10 prompts"
        if complete and isinstance(forecast, (int, float))
        else "forecast unavailable"
    )
    cost_status_value = session.get("cost_status")
    total_text = (
        money(session.get("total_cost_usd"))
        if complete
        else f"known minimum {money(session.get('total_cost_usd'))}"
        if cost_status_value == "partial"
        else "cost unavailable"
    )
    print(f"💸 {total_text} total · {forecast_text}")
    subagents = subagent_usage_text(session)
    if subagents:
        print(subagents)
    print(
        f"🧠 {context_text} session context"
        + (f" · {quota_text}" if quota_text else "")
    )
    norm = baseline_text(session)
    if norm:
        print(norm)


def claude_is_desktop() -> bool:
    """Detect the Claude desktop app, which hides hook system messages in a dropdown."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT") == CLAUDE_DESKTOP_ENTRYPOINT


def codex_is_desktop(transcript: Path | None) -> bool:
    """Detect a Codex desktop client from its rollout metadata, failing closed to the CLI."""
    return transcript is not None and codex_client_in_file(transcript) == "desktop"


def usage_box_lines(session: dict[str, object], quota_text: str) -> list[str]:
    """Build the boxed usage summary shared by every rich display surface."""
    total_cost = session.get("total_cost_usd")
    complete = session.get("cost_status") == "complete"
    forecast = session.get("projected_next_10_tasks_usd")
    forecast_text = (
        f"{money(forecast)} for the next 10 prompts"
        if complete and isinstance(forecast, (int, float))
        else "forecast unavailable"
    )
    total_text = (
        money(total_cost)
        if complete
        else f"known minimum {money(total_cost)}"
        if session.get("cost_status") == "partial"
        else "cost unavailable"
    )
    lines = [
        "╭─ Konvu usage",
        f"│ 💸 {total_text} total · {forecast_text}",
        f"│ 🧠 {context_usage_text(session)}"
        + (f" · {quota_text}" if quota_text else ""),
    ]
    subagents = subagent_usage_text(session)
    if subagents:
        lines.insert(2, f"│ {subagents}")
    norm = baseline_text(session)
    if norm:
        lines.append(f"│ {norm}")
    lines.append("╰─")
    return lines


def prompt_context_payload(session: dict[str, object], quota_text: str) -> str:
    """Serialize the usage box as UserPromptSubmit context the model must echo back."""
    box = "\n".join(usage_box_lines(session, quota_text))
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": f"{PROMPT_BOX_INSTRUCTION}\n\n{box}",
            }
        }
    )


def display_is_worth_showing(provider: str, session: dict[str, object]) -> bool:
    """Gate a rich usage box on a working session that has changed since the last one."""
    total_cost = session.get("total_cost_usd")
    task_count = session.get("task_count")
    return (
        isinstance(total_cost, (int, float))
        and total_cost >= CODEX_DISPLAY_COST_THRESHOLD_USD
        and isinstance(task_count, int)
        and task_count >= CODEX_DISPLAY_MIN_TASKS
        and codex_display_is_worth_showing(session, provider)
    )


def codex_hook() -> None:
    """Return the boxed Codex CLI usage message from its local session file."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print(json.dumps({"suppressOutput": True}))
        return
    if not isinstance(payload, dict):
        print(json.dumps({"suppressOutput": True}))
        return
    session_id: str | None = None
    for key in ("session_id", "thread_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            session_id = value
            break
    if session_id is None or not valid_session_id(session_id):
        print(json.dumps({"suppressOutput": True}))
        return
    turn_id = payload.get("turn_id")
    transcript = codex_hook_transcript(payload, session_id)
    if (
        not isinstance(turn_id, str)
        or transcript is None
        or codex_turn_tool_calls(transcript, turn_id) <= 0
        or codex_is_desktop(transcript)
    ):
        print(json.dumps({"suppressOutput": True}))
        return
    session = refreshed_session("codex", session_id)
    if not isinstance(session, dict) or not display_is_worth_showing("codex", session):
        print(json.dumps({"suppressOutput": True}))
        return
    lines = usage_box_lines(session, recorded_quota_usage_text("codex"))
    print(json.dumps({"systemMessage": "\n" + "\n".join(lines)}))


def claude_hook() -> None:
    """Return a usage message for a Claude Code CLI session."""
    if claude_is_desktop():
        return
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not valid_session_id(session_id):
        return
    session = refreshed_session("claude", session_id)
    if not isinstance(session, dict):
        return
    if not isinstance(session.get("total_cost_usd"), (int, float)):
        return
    lines = usage_box_lines(session, recorded_quota_usage_text("claude"))
    print(json.dumps({"systemMessage": "\n".join(lines)}))


def claude_prompt_hook() -> None:
    """Inject the usage box as visible context for a Claude desktop turn."""
    if not claude_is_desktop():
        return
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not valid_session_id(session_id):
        return
    session = refreshed_session("claude", session_id)
    if not isinstance(session, dict) or not display_is_worth_showing("claude", session):
        return
    print(prompt_context_payload(session, recorded_quota_usage_text("claude")))


def codex_prompt_hook() -> None:
    """Inject the usage box as visible context for a Codex desktop turn."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print(json.dumps({"suppressOutput": True}))
        return
    if not isinstance(payload, dict):
        print(json.dumps({"suppressOutput": True}))
        return
    session_id: str | None = None
    for key in ("session_id", "thread_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            session_id = value
            break
    if session_id is None or not valid_session_id(session_id):
        print(json.dumps({"suppressOutput": True}))
        return
    if not codex_is_desktop(codex_hook_transcript(payload, session_id)):
        print(json.dumps({"suppressOutput": True}))
        return
    session = refreshed_session("codex", session_id)
    if not isinstance(session, dict) or not display_is_worth_showing("codex", session):
        print(json.dumps({"suppressOutput": True}))
        return
    print(prompt_context_payload(session, recorded_quota_usage_text("codex")))


def refreshed_session(provider: str, session_id: str) -> dict[str, object] | None:
    """Read one recent rendered session from the collector snapshot."""
    if provider not in ALLOWED_PROVIDERS or not valid_session_id(session_id):
        return None
    path = session_path(provider, session_id)
    try:
        if time.time() - path.stat().st_mtime > ACTIVITY_FRESHNESS_SECONDS:
            return None
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return (
        {str(key): value for key, value in payload.items()}
        if isinstance(payload, dict)
        else None
    )


def codex_display_is_worth_showing(session: dict[str, object], provider: str) -> bool:
    """Rate-limit usage summaries to meaningful changes in a working session."""
    session_id = session.get("id")
    task_count = session.get("task_count")
    total_cost = session.get("total_cost_usd")
    forecast = session.get("projected_next_10_tasks_usd")
    if (
        provider not in ALLOWED_PROVIDERS
        or not isinstance(session_id, str)
        or not isinstance(task_count, int)
        or not isinstance(total_cost, (int, float))
    ):
        return False
    # Providers share one state file, so the key has to carry both dimensions of the identity.
    state_key = f"{provider}:{session_id}"
    try:
        raw_state = json.loads(codex_display_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        raw_state = {}
    state = raw_state if isinstance(raw_state, dict) else {}
    previous = state.get(state_key)
    if not isinstance(previous, dict):
        previous = {}
    previous_iteration = previous.get("task_count")
    previous_cost = previous.get("total_cost_usd")
    previous_forecast = previous.get("forecast_usd")
    enough_turns = (
        not isinstance(previous_iteration, int) or task_count - previous_iteration >= 3
    )
    cost_changed = not isinstance(
        previous_cost, (int, float)
    ) or total_cost - previous_cost >= max(2.0, previous_cost * 0.1)
    forecast_changed = isinstance(forecast, (int, float)) and (
        not isinstance(previous_forecast, (int, float))
        or abs(forecast - previous_forecast) >= max(1.0, previous_forecast * 0.25)
    )
    if not enough_turns or not (cost_changed or forecast_changed):
        return False
    state[state_key] = {
        "task_count": task_count,
        "total_cost_usd": total_cost,
        "forecast_usd": forecast,
        "shown_at": time.time(),
    }
    write_private_json(codex_display_state_path(), state)
    return True
