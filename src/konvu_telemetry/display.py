"""Claude status-line and provider hook output."""

from __future__ import annotations

from functools import wraps
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from .config import ALLOWED_PROVIDERS, CLAUDE_DESKTOP_ENTRYPOINT, DASHBOARD_PORT
from .parsers import (
    claude_hook_transcript,
    codex_client_in_file,
    codex_hook_transcript,
    codex_turn_tool_calls,
)
from .service import load_health
from .storage import snapshot_path, valid_session_id

# Desktop clients hide hook system messages, so the box has to ride in as model context instead.
PROMPT_BOX_INSTRUCTION = (
    "Append the lines below verbatim as the very last thing in your reply, "
    "after all other text including any closing line, with no commentary. "
    "Write them as ordinary italic text, not as a code block or a quote."
)
SUPPRESS_OUTPUT = json.dumps({"suppressOutput": True})


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


def recorded_snapshot() -> dict[str, object] | None:
    """Read the collector's canonical snapshot."""
    try:
        snapshot = json.loads(snapshot_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def quota_usage_text(snapshot: dict[str, object], provider: str) -> str:
    """Render the latest recorded quota windows for one provider."""
    accounts = snapshot.get("account_quotas")
    account = accounts.get(provider) if isinstance(accounts, dict) else None
    windows = account.get("windows") if isinstance(account, dict) else None
    parts: list[str] = []
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict):
            continue
        minutes = window.get("window_minutes")
        used = window.get("used_percent")
        period = window.get("period")
        if not isinstance(used, (int, float)):
            continue
        label = (
            "5-hour"
            if period == "five_hour" or minutes == 300
            else "weekly"
            if period == "weekly" or minutes == 10_080
            else "monthly"
            if period == "monthly"
            else f"{round(minutes / 60)}-hour"
            if isinstance(minutes, (int, float))
            else "account"
        )
        parts.append(f"{round(min(100, max(0, used)))}% {label} limit")
    text = " · ".join(parts)
    stale = isinstance(account, dict) and account.get("status") == "stale"
    return f"Last known · {text}" if text and stale else text


def recorded_quota_usage_text(provider: str) -> str:
    """Read the collector's latest quota windows for a provider hook."""
    snapshot = recorded_snapshot()
    return quota_usage_text(snapshot, provider) if snapshot is not None else ""


def subagent_usage_text(session: dict[str, object]) -> str:
    """Summarise the live and total child-agent footprint in one short line."""
    total = session.get("subagent_total")
    live = session.get("active_subagents")
    handed = session.get("subagent_entry_context_tokens")
    context = session.get("context_tokens")
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
    spend_text = f"{money(session.get('subagent_cost_usd'))} API-equivalent"
    return f"🤖 {live or 0} live / {total} total · {shared_text} · {spend_text}"


def dashboard_line() -> str:
    """Link the local dashboard, or name the command that starts it when it is not serving."""
    # Only a healthy collector is serving the dashboard; a wrong URL is worse than a hint.
    try:
        reachable = load_health().get("status") == "healthy"
    except Exception:
        reachable = False
    return (
        f"🔗 dashboard: http://127.0.0.1:{DASHBOARD_PORT}/"
        if reachable
        else "🔗 run konvu-telemetry setup to start the dashboard"
    )


def context_usage_text(session: dict[str, object]) -> str:
    """Render context as a percentage when the provider exposes its window size."""
    context = session.get("context_tokens")
    window = session.get("context_window_tokens")
    if isinstance(context, int) and isinstance(window, int) and window > 0:
        return f"{round(context / window * 100)}% context"
    return f"{tokens(context)} context"


def quota_attribution_text(session: dict[str, object]) -> str:
    """Render the current session's explicitly estimated subscription share."""
    attribution = session.get("quota_attribution")
    windows = attribution.get("windows") if isinstance(attribution, dict) else None
    provider = session.get("provider")
    target_period = "weekly" if provider == "codex" else "five_hour"
    parts: list[str] = []
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict):
            continue
        period = window.get("period")
        estimate = window.get("estimated_percent")
        if period != target_period or not isinstance(estimate, (int, float)):
            continue
        label = "5-hour" if period == "five_hour" else period
        hot = (provider == "claude" and period == "five_hour" and estimate > 20) or (
            provider == "codex" and period == "weekly" and estimate > 10
        )
        parts.append(
            f"~{float(estimate):.1f}% of {label} limit" + (" 🔥" if hot else "")
        )
    return " · ".join(parts)


def quota_forecast_text(session: dict[str, object]) -> str:
    """Render a calibrated next-ten subscription-limit estimate when available."""
    attribution = session.get("quota_attribution")
    windows = attribution.get("windows") if isinstance(attribution, dict) else None
    rows = (
        [row for row in windows if isinstance(row, dict)]
        if isinstance(windows, list)
        else []
    )
    for period in ("five_hour", "weekly"):
        for window in rows:
            forecast = window.get("projected_next_10_percent")
            if window.get("period") == period and isinstance(forecast, (int, float)):
                label = "5-hour" if period == "five_hour" else "weekly"
                return f"next 10: ~{float(forecast):.1f}% of {label} limit"
    return ""


def usage_rows(
    session: dict[str, object],
    quota_text: str,
    context_percent: float | None = None,
) -> list[str]:
    """Build the usage summary every surface shows, unframed; each surface wraps it itself."""
    usage_mode = session.get("usage_mode")
    complete = session.get("cost_status") == "complete"
    forecast = session.get("projected_next_10_tasks_usd")
    forecast_text = (
        f"{money(forecast)} API-equivalent for the next 10 prompts"
        if complete and isinstance(forecast, (int, float))
        else "forecast unavailable"
    )
    total_cost = (
        session.get("total_cost_usd")
        if usage_mode == "api_billed"
        else session.get("out_of_plan_spend_usd")
        if session.get("out_of_plan_spend_status") is not None
        else None
    )
    total_text = (
        f"{money(total_cost)} API-equivalent"
        if complete and isinstance(total_cost, (int, float))
        else "— spent beyond plan"
        if complete
        else f"known minimum {money(total_cost)} API-equivalent"
        if session.get("cost_status") == "partial"
        else "cost unavailable"
    )
    context_text = (
        f"{context_percent:.0f}% context"
        if context_percent is not None
        else context_usage_text(session)
    )
    money_visible = usage_mode in {"api_billed", "exhausted"}
    quota_stale = session.get("quota_status") == "stale"
    included_label = "🟡 Last known: included" if quota_stale else "🟢 Included"
    money_label = "🟡 Last known plan status · " if quota_stale else "💸 "
    rows = (
        [f"{money_label}{total_text} total · {forecast_text}"]
        if money_visible
        else [f"{included_label} · {quota_forecast_text(session)}".rstrip(" ·")]
        if usage_mode == "included"
        else ["⚪ Subscription limit unavailable"]
    )
    subagents = subagent_usage_text(session)
    if subagents and money_visible:
        rows.append(subagents)
    context_row = f"🧠 {context_text}"
    if usage_mode == "included":
        if quota_text:
            rows.append(quota_text)
        attribution = quota_attribution_text(session)
        if attribution:
            context_row += f" · Responsible for {attribution}"
    elif quota_text:
        context_row += f" · {quota_text}"
    rows.append(context_row)
    rows.append(dashboard_line())
    return rows


def payload_context_percent(payload: dict[str, object]) -> float | None:
    """Read the live context percentage Claude reports to the status-line hook."""
    context = payload.get("context_window")
    value = context.get("used_percentage") if isinstance(context, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


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
    session = refreshed_session("claude", session_id)
    if session is None:
        print("Konvu live usage: collector starting")
        return
    # Claude's context is live, but its rate-limit payload can lag the provider API.
    rows = usage_rows(
        session,
        recorded_quota_usage_text("claude"),
        payload_context_percent(payload),
    )
    for row in rows:
        print(row)


def claude_is_desktop() -> bool:
    """Detect the Claude desktop app, which hides hook system messages in a dropdown."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT") == CLAUDE_DESKTOP_ENTRYPOINT


def codex_is_desktop(transcript: Path | None) -> bool:
    """Detect a Codex desktop client from its rollout metadata, failing closed to the CLI."""
    return transcript is not None and codex_client_in_file(transcript) == "desktop"


def usage_box_lines(session: dict[str, object], quota_text: str) -> list[str]:
    """Frame the shared usage rows for the Codex Stop hook and both prompt hooks."""
    rows = usage_rows(session, quota_text)
    return ["╭─ Konvu usage", *(f"│ {row}" for row in rows), "╰─"]


def prompt_box_context(provider: str, session_id: str) -> str | None:
    """Serialize the usage box as UserPromptSubmit context, or None when it stays hidden."""
    session = refreshed_session(provider, session_id)
    if not isinstance(session, dict) or not last_prompt_used_a_tool(session):
        return None
    body = "\n".join(usage_box_lines(session, recorded_quota_usage_text(provider)))
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": f"{PROMPT_BOX_INSTRUCTION}\n\n{body}",
            }
        }
    )


def codex_hook_request() -> tuple[dict[str, object], str] | None:
    """Read a Codex hook's stdin payload and the valid session identity it names."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("session_id", "thread_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return (payload, value) if valid_session_id(value) else None
    return None


def last_prompt_used_a_tool(session: dict[str, object]) -> bool:
    """Show the usage box only for a prompt that actually put the model to work."""
    tool_calls = session.get("last_task_tool_calls")
    if isinstance(tool_calls, bool) or not isinstance(tool_calls, int):
        return False
    return tool_calls > 0


def silent_hook(hook: Callable[[], None]) -> Callable[[], None]:
    """Keep a failed usage display from blocking the prompt that triggered it."""

    @wraps(hook)
    def guarded() -> None:
        try:
            hook()
        except Exception:
            return

    return guarded


@silent_hook
def codex_hook() -> None:
    """Return the boxed Codex CLI usage message from its local session file."""
    request = codex_hook_request()
    if request is None:
        print(SUPPRESS_OUTPUT)
        return
    payload, session_id = request
    turn_id = payload.get("turn_id")
    transcript = codex_hook_transcript(payload, session_id)
    if (
        not isinstance(turn_id, str)
        or transcript is None
        or codex_turn_tool_calls(transcript, turn_id) <= 0
        or codex_is_desktop(transcript)
    ):
        print(SUPPRESS_OUTPUT)
        return
    session = refreshed_session("codex", session_id)
    if not isinstance(session, dict):
        print(SUPPRESS_OUTPUT)
        return
    lines = usage_box_lines(session, recorded_quota_usage_text("codex"))
    print(json.dumps({"systemMessage": "\n" + "\n".join(lines)}))


@silent_hook
def claude_hook() -> None:
    """Accept the Claude Stop hook without output; the status line reports CLI usage."""
    # Retained so settings written by older versions keep working instead of erroring.
    return


@silent_hook
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
    context = prompt_box_context("claude", session_id)
    if context is not None:
        print(context)


@silent_hook
def codex_prompt_hook() -> None:
    """Inject the usage box as visible context for a Codex desktop turn."""
    request = codex_hook_request()
    if request is None:
        print(SUPPRESS_OUTPUT)
        return
    payload, session_id = request
    if not codex_is_desktop(codex_hook_transcript(payload, session_id)):
        print(SUPPRESS_OUTPUT)
        return
    context = prompt_box_context("codex", session_id)
    print(SUPPRESS_OUTPUT if context is None else context)


def refreshed_session(provider: str, session_id: str) -> dict[str, object] | None:
    """Read one session from the collector's canonical fleet snapshot."""
    if provider not in ALLOWED_PROVIDERS or not valid_session_id(session_id):
        return None
    snapshot = recorded_snapshot()
    if snapshot is None:
        return None
    sessions = snapshot.get("sessions")
    for payload in sessions if isinstance(sessions, list) else []:
        if (
            isinstance(payload, dict)
            and payload.get("provider") == provider
            and payload.get("id") == session_id
        ):
            return {str(key): value for key, value in payload.items()}
    return None
