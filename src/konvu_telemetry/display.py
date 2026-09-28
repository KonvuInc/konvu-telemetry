"""Claude status-line and provider hook output."""

from __future__ import annotations

from functools import wraps
import json
import math
import os
import re
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
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
from .provider_limits import stored_provider_quotas
from .service import load_health
from .storage import session_path, snapshot_path, valid_session_id

# Desktop clients hide hook system messages, so the box has to ride in as model context instead.
PROMPT_BOX_INSTRUCTION = (
    "Append the lines below verbatim as the very last thing in your reply, "
    "after all other text including any closing line, with no commentary. "
    "Write them as ordinary italic text, not as a code block or a quote."
)
SUPPRESS_OUTPUT = json.dumps({"suppressOutput": True})
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def one_decimal(value: int | float) -> str:
    """Round a finite display value to one decimal using half-up semantics."""
    return str(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def money(value: object) -> str:
    return (
        f"${one_decimal(value)}"
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        else "price unavailable"
    )


def percentage(value: object) -> str:
    """Format a displayed percentage consistently across CLI surfaces."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{one_decimal(value)}%" if math.isfinite(value) else "—"


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
        emoji = (
            "⏳"
            if label == "5-hour"
            else "📅"
            if label == "weekly"
            else "🌙"
            if label == "monthly"
            else ""
        )
        parts.append(
            f"{emoji} {percentage(min(100, max(0, used)))} {label} limit".lstrip()
        )
    text = " · ".join(parts)
    stale = isinstance(account, dict) and account.get("status") == "stale"
    return f"Last known · {text}" if text and stale else text


def recorded_quota_usage_text(provider: str) -> str:
    """Read the collector's latest quota windows for a provider hook."""
    return quota_usage_text({"account_quotas": stored_provider_quotas()}, provider)


def subagent_usage_text(session: dict[str, object]) -> str:
    """Summarise the live and total child-agent footprint in one short line."""
    total = session.get("subagent_total")
    live = session.get("active_subagents")
    handed = session.get(
        "subagent_context_tokens", session.get("subagent_entry_context_tokens")
    )
    context = session.get("context_tokens")
    if not isinstance(total, int) or total == 0:
        return ""
    shared_percentage = None
    if isinstance(handed, int) and isinstance(context, int) and context > 0:
        shared_percentage = handed / (context * total) * 100
    shared_text = (
        f"{percentage(min(100, shared_percentage))} context shared"
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
        return f"{percentage(context / window * 100)} context"
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
            f"~{percentage(estimate)} of {label} limit" + (" 🔥" if hot else "")
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
                return (
                    f"~{percentage(forecast)} of {label} limit in the next 10 prompts"
                )
    return ""


def terminal_style(text: str, code: str) -> str:
    """Apply ANSI color unless the terminal explicitly disables it."""
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def terminal_link(text: str, url: str) -> str:
    """Wrap text in an OSC 8 terminal hyperlink when supported."""
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return text
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


def percentage_color(value: float) -> str:
    """Choose a legible terminal color for a bounded percentage."""
    return "38;5;203" if value >= 85 else "38;5;221" if value >= 60 else "38;5;78"


def battery_meter(value: float, width: int = 7) -> str:
    """Render the compact battery cells used by the Claude CLI HUD."""
    filled = round(min(100.0, max(0.0, value)) / 100 * width)
    return terminal_style("■" * filled, percentage_color(value)) + terminal_style(
        "□" * (width - filled), "38;5;240"
    )


def statusline_width() -> int:
    """Read Claude's width hint with a conservative fallback."""
    try:
        columns = int(os.environ.get("COLUMNS", "80"))
    except ValueError:
        return 76
    return max(32, columns - 4)


def quota_window_value(
    session: dict[str, object], period: str, key: str
) -> float | None:
    """Read one estimated quota value for a provider window."""
    attribution = session.get("quota_attribution")
    windows = attribution.get("windows") if isinstance(attribution, dict) else None
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict) or window.get("period") != period:
            continue
        value = window.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def claude_quota_values(snapshot: dict[str, object] | None) -> dict[str, float]:
    """Read the five-hour and weekly Claude account percentages."""
    accounts = snapshot.get("account_quotas") if isinstance(snapshot, dict) else None
    account = accounts.get("claude") if isinstance(accounts, dict) else None
    windows = account.get("windows") if isinstance(account, dict) else None
    values: dict[str, float] = {}
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict):
            continue
        period, used = window.get("period"), window.get("used_percent")
        if period in {"five_hour", "weekly"} and isinstance(used, (int, float)):
            values[str(period)] = min(100.0, max(0.0, float(used)))
    return values


def meter_segment(label: str, value: float, width: int) -> str:
    """Render one labeled responsive status meter."""
    return (
        terminal_style(label, "38;5;245")
        + " "
        + battery_meter(value, width)
        + " "
        + terminal_style(f"{value:.0f}%", f"1;{percentage_color(value)}")
    )


def wrap_statusline_segments(segments: list[str], width: int) -> list[str]:
    """Wrap complete HUD cells without dropping context on narrow terminals."""
    separator = terminal_style("  ·  ", "38;5;245")
    rows: list[str] = []
    current = ""
    for segment in segments:
        candidate = segment if not current else current + separator + segment
        if current and len(ANSI_ESCAPE.sub("", candidate)) > width:
            rows.append(current)
            current = segment
        else:
            current = candidate
    if current:
        rows.append(current)
    return rows


def cli_dashboard_line() -> str:
    """Render a styled dashboard link only while the local collector is healthy."""
    line = dashboard_line()
    prefix = "🔗 dashboard: "
    if not line.startswith(prefix):
        return line
    url = line.removeprefix(prefix)
    label = terminal_style("🔗 OPEN LIVE DASHBOARD", "38;5;245")
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return f"{label} {url}"
    return terminal_link(label, url)


def claude_statusline_rows(
    session: dict[str, object], context_percent: float | None
) -> list[str]:
    """Render the responsive Claude CLI-only usage HUD from real session data."""
    usage_mode = session.get("usage_mode")
    dashboard = cli_dashboard_line()
    if usage_mode == "included":
        quotas = claude_quota_values({"account_quotas": stored_provider_quotas()})
        context = context_percent
        if context is None:
            raw_context, raw_window = (
                session.get("context_tokens"),
                session.get("context_window_tokens"),
            )
            if (
                isinstance(raw_context, int)
                and isinstance(raw_window, int)
                and raw_window > 0
            ):
                context = raw_context / raw_window * 100
        width = statusline_width()
        cells = 4 if width < 62 else 6 if width < 84 else 8
        segments = [terminal_style("● INCLUDED", "1;38;5;78")]
        for label, period in (("5H", "five_hour"), ("WEEK", "weekly")):
            value = quotas.get(period)
            if value is not None:
                segments.append(meter_segment(label, value, cells))
        if context is not None:
            segments.append(meter_segment("CONTEXT", context, cells))
        rows = wrap_statusline_segments(segments, width)
        attribution = quota_window_value(session, "five_hour", "estimated_percent")
        forecast = quota_window_value(session, "five_hour", "projected_next_10_percent")
        if attribution is not None and forecast is not None:
            projected = min(100.0, attribution + forecast)
            forecast_row = (
                terminal_style("THIS SESSION", "38;5;245")
                + " "
                + terminal_style(f"{attribution:.1f}%", "1;38;5;255")
                + " "
                + terminal_style("OF 5H LIMIT", "38;5;245")
                + " "
                + terminal_style("━━━▶", "1;38;5;141")
                + " "
                + terminal_style(f"{projected:.1f}%", "1;38;5;255")
                + " "
                + terminal_style("FORECASTED IN NEXT 10 PROMPTS", "38;5;245")
            )
            rows.append(forecast_row)
        if width >= 45:
            rows.append(dashboard)
        return rows
    if usage_mode in {"api_billed", "exhausted"}:
        paid = session.get("total_cost_usd")
        out_of_plan = session.get("out_of_plan_spend_usd")
        if usage_mode == "exhausted" and isinstance(out_of_plan, (int, float)):
            paid = out_of_plan
        paid_forecast = session.get("projected_next_10_tasks_usd")
        rows = [terminal_style("● PAYING", "1;38;5;203")]
        if isinstance(paid, (int, float)) and isinstance(paid_forecast, (int, float)):
            rows.append(
                terminal_style("CURRENT SPEND", "38;5;245")
                + " "
                + terminal_style(money(paid), "1;38;5;255")
                + " "
                + terminal_style("━━━▶", "1;38;5;141")
                + " "
                + terminal_style(money(paid + paid_forecast), "1;38;5;255")
                + " "
                + terminal_style("FORECASTED IN NEXT 10 PROMPTS", "38;5;245")
            )
        rows.append(dashboard)
        return rows
    return usage_rows(session, recorded_quota_usage_text("claude"), context_percent)


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
        f"{percentage(context_percent)} context"
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
            context_row += f" · 🎯 Responsible for {attribution}"
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
        session = retained_session("claude", session_id)
    if session is None:
        print("Konvu live usage: collector starting")
        return
    # Claude's context is live, but its rate-limit payload can lag the provider API.
    rows = claude_statusline_rows(session, payload_context_percent(payload))
    for row in rows:
        print(row)


def claude_is_desktop() -> bool:
    """Detect the Claude desktop app, which hides hook system messages in a dropdown."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT") == CLAUDE_DESKTOP_ENTRYPOINT


def codex_is_desktop(transcript: Path | None) -> bool:
    """Detect a Codex desktop client from its rollout metadata, failing closed to the CLI."""
    return transcript is not None and codex_client_in_file(transcript) == "desktop"


def meter(value: object, width: int = 10) -> str:
    """Render a compact monochrome percentage meter for hook-only output."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "░" * width
    bounded = min(100.0, max(0.0, float(value)))
    filled = min(width, max(0, round(bounded / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def hook_quota_meters(quota_text: str) -> str:
    """Convert recorded provider limit text into small, readable box meters."""
    labels = {"5-hour": "5H", "weekly": "WEEK", "monthly": "MONTH"}
    matches = re.findall(r"(\d+(?:\.\d+)?)% (5-hour|weekly|monthly) limit", quota_text)
    return "  ".join(
        f"{labels[label]} [{meter(float(used), 7)}] {percentage(float(used))}"
        for used, label in matches
    )


def hook_forecast_row(session: dict[str, object]) -> str:
    """Render the same session-to-forecast arrow used by the terminal HUD."""
    usage_mode = session.get("usage_mode")
    if usage_mode in {"api_billed", "exhausted"}:
        current = session.get("total_cost_usd")
        out_of_plan = session.get("out_of_plan_spend_usd")
        if usage_mode == "exhausted" and isinstance(out_of_plan, (int, float)):
            current = out_of_plan
        forecast = session.get("projected_next_10_tasks_usd")
        if isinstance(current, (int, float)) and isinstance(forecast, (int, float)):
            return (
                f"💸 CURRENT SPEND {money(current)}  ━━━▶  {money(current + forecast)} "
                "FORECASTED IN NEXT 10 PROMPTS"
            )
        return "💸 FORECAST UNAVAILABLE"
    attribution = session.get("quota_attribution")
    windows = attribution.get("windows") if isinstance(attribution, dict) else None
    target = "weekly" if session.get("provider") == "codex" else "five_hour"
    label = "WEEKLY" if target == "weekly" else "5H"
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict) or window.get("period") != target:
            continue
        current = window.get("estimated_percent")
        forecast = window.get("projected_next_10_percent")
        if isinstance(current, (int, float)) and isinstance(forecast, (int, float)):
            projected_total = min(100.0, current + forecast)
            return (
                f"📈 THIS SESSION {percentage(current)} OF {label} LIMIT  ━━━▶  "
                f"{percentage(projected_total)} FORECASTED IN NEXT 10 PROMPTS"
            )
    return "📈 SUBSCRIPTION FORECAST UNAVAILABLE"


def usage_box_lines(session: dict[str, object], quota_text: str) -> list[str]:
    """Frame text-only meters and forecast data for Codex and desktop hooks."""
    included = session.get("usage_mode") == "included"
    rows = ["🟢 INCLUDED" if included else "🔴 PAYING"]
    context = context_usage_text(session)
    context_percent = session.get("context_tokens")
    window = session.get("context_window_tokens")
    if isinstance(context_percent, int) and isinstance(window, int) and window > 0:
        used = context_percent / window * 100
        context_row = f"CONTEXT [{meter(used, 7)}] {percentage(used)}"
    else:
        context_row = f"CONTEXT {context}"
    quota_meters = hook_quota_meters(quota_text)
    meter_row = f"{quota_meters}  {context_row}" if quota_meters else context_row
    rows.append(f"⏱️ {meter_row}")
    rows.append(hook_forecast_row(session))
    rows.append(dashboard_line().replace("dashboard:", "OPEN LIVE DASHBOARD"))
    return ["╭─ KONVU USAGE", *(f"│ {row}" for row in rows), "╰─"]


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


def retained_session(provider: str, session_id: str) -> dict[str, object] | None:
    """Read a collector-written session filtered from the live dashboard summary."""
    if provider not in ALLOWED_PROVIDERS or not valid_session_id(session_id):
        return None
    try:
        payload = json.loads(session_path(provider, session_id).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if (
        isinstance(payload, dict)
        and payload.get("provider") == provider
        and payload.get("id") == session_id
    ):
        return {str(key): value for key, value in payload.items()}
    return None
