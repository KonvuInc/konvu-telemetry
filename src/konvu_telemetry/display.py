"""Claude status-line and provider hook output."""

from __future__ import annotations

import importlib.util

from functools import wraps
import json
import math
import os
import re
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from .config import ALLOWED_PROVIDERS, CLAUDE_DESKTOP_ENTRYPOINT, DASHBOARD_PORT
from .parsers import (
    claude_hook_transcript,
    codex_client_in_file,
    codex_hook_transcript,
    codex_turn_tool_calls,
)
from .provider_limits import stored_provider_quotas
from .service import load_health
from .preferences import read_preferences
from .storage import (
    custom_rule_module_path,
    home_dir,
    session_path,
    snapshot_path,
    valid_session_id,
    write_private_json,
)

# Desktop clients hide hook system messages, so the box has to ride in as model context instead.
PROMPT_BOX_INSTRUCTION = (
    "Append the lines below verbatim as the very last thing in your reply, "
    "after all other text including any closing line, with no commentary. "
    "Write them as ordinary italic text, not as a code block or a quote."
)
PROMPT_BOX_SUPPRESSION_INSTRUCTION = (
    "Do not display, repeat, or mention a Konvu usage box in this reply. "
    "This instruction overrides any earlier Konvu usage-box instruction."
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


def compact_status_meter(
    label: str, value: float, width: int, inset: str | None = None
) -> str:
    """Render a cell-colored meter with optional centered reset text."""
    if not inset or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return meter_segment(label, value, width)
    bounded = min(100.0, max(0.0, value))
    filled = min(width, max(0, round(bounded / 100 * width)))
    text = f"reset: {inset}"[:width].center(width)
    fill_code = percentage_color(value).removeprefix("38;")
    cells = "".join(
        terminal_style(
            character,
            f"48;{fill_code};38;5;16" if index < filled else "48;5;238;38;5;245",
        )
        for index, character in enumerate(text)
    )
    return (
        terminal_style(label, "38;5;245")
        + " "
        + cells
        + " "
        + terminal_style(f"{value:.0f}%", f"1;{percentage_color(value)}")
    )


RELEVANCE_COLORS = (
    ("relevant_percent", "38;5;78"),
    ("drifting_percent", "38;5;221"),
    ("stale_percent", "38;5;203"),
)
# Suggest /compact once compactable context fills this many points of the window.
COMPACT_RECOMMEND_WINDOW_POINTS = 20.0
COMPACT_MIN_COVERAGE_PERCENT = 70.0


def context_analysis(session: dict[str, object]) -> dict[str, object] | None:
    """The session's AI relevance summary, once at least one pass has rated it."""
    context_map = session.get("context_map")
    analysis = context_map.get("analysis") if isinstance(context_map, dict) else None
    if not isinstance(analysis, dict) or not analysis.get("coverage_percent"):
        return None
    return analysis


def relevance_context_meter(
    label: str, value: float, width: int, analysis: dict[str, object]
) -> str:
    """Render used context split into needed, compactable and unreviewed cells."""
    bounded = min(100.0, max(0.0, value))
    filled = min(width, max(1 if bounded > 0 else 0, round(bounded / 100 * width)))
    shares = [
        max(0.0, float(raw)) if isinstance(raw, (int, float)) else 0.0
        for key, _ in RELEVANCE_COLORS
        for raw in [analysis.get(key)]
    ]
    shares.append(max(0.0, 100.0 - sum(shares)))
    exact = [share / 100 * filled for share in shares]
    counts = [math.floor(part) for part in exact]
    # Largest remainders get the leftover cells so the used cells always add up.
    for index in sorted(
        range(len(exact)), key=lambda i: exact[i] - counts[i], reverse=True
    )[: filled - sum(counts)]:
        counts[index] += 1
    colors = [code for _, code in RELEVANCE_COLORS] + ["38;5;245"]
    # A thin continuous bar reads as one stacked gauge instead of separate battery cells.
    cells = "".join(
        terminal_style("━" * count, f"1;{color}")
        for count, color in zip(counts, colors)
    ) + terminal_style("─" * (width - filled), "38;5;238")
    return (
        terminal_style(label, "38;5;245")
        + " "
        + cells
        + " "
        + terminal_style(f"{value:.0f}%", f"1;{percentage_color(value)}")
    )


def compact_worthwhile(analysis: dict[str, object], used_percent: float) -> bool:
    """Whether clearly dead context fills enough of the window to be worth compacting.

    Only scores 0-3 count: "not needed now" context may still be looked up again.
    """
    raw = analysis.get("droppable_percent")
    waste = float(raw) if isinstance(raw, (int, float)) else 0.0
    coverage = analysis.get("coverage_percent")
    # Advice built on a small reviewed slice of the window is not shown.
    if (
        not isinstance(coverage, (int, float))
        or coverage < COMPACT_MIN_COVERAGE_PERCENT
    ):
        return False
    return (
        waste / 100 * min(100.0, max(0.0, used_percent))
        >= COMPACT_RECOMMEND_WINDOW_POINTS
    )


def compact_advice(session: dict[str, object], used_percent: float) -> str | None:
    """A clickable /compact call to action that opens the panel holding the full command."""
    analysis = context_analysis(session)
    command = analysis.get("compact_command") if analysis else None
    if not analysis or not command or not compact_worthwhile(analysis, used_percent):
        return None
    url = (
        f"http://127.0.0.1:{DASHBOARD_PORT}/?session="
        + quote(f"{session.get('provider') or 'claude'}:{session.get('id')}", safe="")
        + "&tab=context"
    )
    # Only the command word is colored; the terminal's own link styling marks it clickable.
    return terminal_link(terminal_style("/compact", "38;5;141"), url) + terminal_style(
        " suggested", "38;5;245"
    )


def context_meter(session: dict[str, object], value: float) -> str:
    """Always draw the thin relevance bar; unrated context renders grey until ratings exist."""
    analysis = context_analysis(session)
    meter = relevance_context_meter("Context", value, 16, analysis or {})
    advice = compact_advice(session, value)
    return meter + (terminal_style("  ·  ", "38;5;245") + advice if advice else "")


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
    label = terminal_style("🔗 Open live dashboard", "38;5;245")
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
        cells = 13 if width < 62 else 14
        resets = quota_reset_times("claude")
        quota_stale = session.get("quota_status") == "stale"
        included_label = (
            "● Last known: included · retrying" if quota_stale else "● Included"
        )
        segments = [
            terminal_style(included_label, "1;38;5;221" if quota_stale else "1;38;5;78")
        ]
        for label, period in (("5h", "five_hour"), ("Week", "weekly")):
            value = quotas.get(period)
            if value is not None:
                segments.append(
                    compact_status_meter(label, value, cells, resets.get(period))
                )
        rows = wrap_statusline_segments(segments, width)
        # Context always gets its own second line so the /compact hint sits beside it.
        if context is not None:
            rows.append(context_meter(session, context))
        attribution = quota_window_value(session, "five_hour", "estimated_percent")
        forecast = quota_window_value(session, "five_hour", "projected_next_10_percent")
        if attribution is not None and forecast is not None:
            projected = min(100.0, attribution + forecast)
            forecast_row = (
                terminal_style("This session", "38;5;245")
                + " "
                + terminal_style(f"{attribution:.1f}%", "1;38;5;255")
                + " "
                + terminal_style("of 5h limit", "38;5;245")
                + " "
                + terminal_style("━━━▶", "1;38;5;141")
                + " "
                + terminal_style(f"{projected:.1f}%", "1;38;5;255")
                + " "
                + terminal_style("forecasted in next 10 prompts", "38;5;245")
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
        reset = paying_reset_in("claude") if usage_mode == "exhausted" else None
        paying_label = "● Paying" + (f" · resets in {reset}" if reset else "")
        rows = [terminal_style(paying_label, "1;38;5;203")]
        context = context_percent
        if context is None:
            raw_context = session.get("context_tokens")
            raw_window = session.get("context_window_tokens")
            if (
                isinstance(raw_context, int)
                and isinstance(raw_window, int)
                and raw_window > 0
            ):
                context = raw_context / raw_window * 100
        if context is not None:
            rows.append(context_meter(session, context))
        if isinstance(paid, (int, float)) and isinstance(paid_forecast, (int, float)):
            rows.append(
                terminal_style("Current spend", "38;5;245")
                + " "
                + terminal_style(money(paid), "1;38;5;255")
                + " "
                + terminal_style("━━━▶", "1;38;5;141")
                + " "
                + terminal_style(money(paid + paid_forecast), "1;38;5;255")
                + " "
                + terminal_style("forecasted in next 10 prompts", "38;5;245")
            )
        rows.append(dashboard)
        return rows
    context = context_percent
    if context is None:
        raw_context = session.get("context_tokens")
        raw_window = session.get("context_window_tokens")
        if (
            isinstance(raw_context, int)
            and isinstance(raw_window, int)
            and raw_window > 0
        ):
            context = raw_context / raw_window * 100
    rows = [
        terminal_style("● Subscription limits unavailable · retrying", "1;38;5;245")
    ]
    if context is not None:
        rows.append(context_meter(session, context))
    rows.append(terminal_style("📈 Subscription forecast unavailable", "38;5;245"))
    if statusline_width() >= 45:
        rows.append(dashboard)
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


def reset_in(value: object) -> str | None:
    """Format a provider reset timestamp as a compact remaining duration."""
    if not isinstance(value, str):
        return None
    try:
        seconds = max(
            0,
            int(
                datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                - time.time()
            ),
        )
    except ValueError:
        return None
    return compact_duration(seconds)


def compact_duration(seconds: int | float) -> str:
    """Render a positive duration in its largest unit without a leading zero."""
    remaining = max(0.0, float(seconds))
    for unit, size in (("d", 86_400), ("h", 3_600), ("m", 60), ("s", 1)):
        if remaining >= size or unit == "s":
            amount = one_decimal(remaining / size).removesuffix(".0")
            return f"{amount}{unit}"
    return "0s"


def quota_reset_times(provider: str) -> dict[str, str]:
    """Read current provider reset timers keyed by quota period."""
    account = stored_provider_quotas().get(provider)
    windows = account.get("windows") if isinstance(account, dict) else None
    resets: dict[str, str] = {}
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict) or not isinstance(window.get("period"), str):
            continue
        remaining = reset_in(window.get("resets_at"))
        if remaining is not None:
            resets[window["period"]] = remaining
    return resets


def paying_reset_in(provider: str) -> str | None:
    """Return when the exhausted ordinary quota windows will all have reset."""
    account = stored_provider_quotas().get(provider)
    windows = account.get("windows") if isinstance(account, dict) else None
    provider_exhausted = (
        isinstance(account, dict) and account.get("ordinary_usage_allowed") is False
    )
    candidates: list[tuple[float, str]] = []
    exhausted: list[tuple[float, str]] = []
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict) or window.get("period") not in {
            "five_hour",
            "weekly",
        }:
            continue
        resets_at = window.get("resets_at")
        if not isinstance(resets_at, str):
            continue
        try:
            timestamp = datetime.fromisoformat(
                resets_at.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            continue
        candidates.append((timestamp, resets_at))
        used = window.get("used_percent")
        if (
            isinstance(used, (int, float))
            and not isinstance(used, bool)
            and used >= 100
        ):
            exhausted.append((timestamp, resets_at))
    applicable = exhausted
    if not applicable and provider_exhausted:
        applicable = candidates
    if not applicable:
        return None
    return reset_in(max(applicable)[1])


def hook_quota_meters(
    quota_text: str,
    provider: str,
    include_monthly: bool = False,
    include_plan: bool = True,
    include_resets: bool = True,
) -> str:
    """Convert the applicable recorded limits into small, readable box meters."""
    labels = {"5-hour": "5h", "weekly": "Week", "monthly": "Credits"}
    matches = re.findall(r"(\d+(?:\.\d+)?)% (5-hour|weekly|monthly) limit", quota_text)
    resets = quota_reset_times(provider)
    periods = {"5-hour": "five_hour", "weekly": "weekly", "monthly": "monthly"}
    return "  ".join(
        f"{labels[label]}"
        + (
            f" · reset: {resets[periods[label]]}"
            if include_resets and periods[label] in resets
            else ""
        )
        + f" [{meter(float(used), 7)}] {percentage(float(used))}"
        for used, label in matches
        if (label == "monthly" and include_monthly)
        or (label != "monthly" and include_plan)
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
                f"💸 Current spend {money(current)}  ━━━▶  {money(current + forecast)} "
                "forecasted in next 10 prompts"
            )
        return "💸 Forecast unavailable"
    attribution = session.get("quota_attribution")
    windows = attribution.get("windows") if isinstance(attribution, dict) else None
    target = "weekly" if session.get("provider") == "codex" else "five_hour"
    label = "weekly" if target == "weekly" else "5h"
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict) or window.get("period") != target:
            continue
        current = window.get("estimated_percent")
        forecast = window.get("projected_next_10_percent")
        if isinstance(current, (int, float)) and isinstance(forecast, (int, float)):
            projected_total = min(100.0, current + forecast)
            return (
                f"📈 This session {percentage(current)} of {label} limit  ━━━▶  "
                f"{percentage(projected_total)} forecasted in next 10 prompts"
            )
    return "📈 Subscription forecast unavailable"


def usage_box_lines(
    session: dict[str, object], quota_text: str, provider: str = ""
) -> list[str]:
    """Frame text-only meters and forecast data for Codex and desktop hooks."""
    usage_mode = session.get("usage_mode")
    paying = usage_mode in {"api_billed", "exhausted"}
    if not provider and isinstance(session.get("provider"), str):
        provider = str(session["provider"])
    reset = paying_reset_in(provider) if usage_mode == "exhausted" else None
    rows = (
        ["🟢 Included"]
        if usage_mode == "included"
        else ["🔴 Paying" + (f" · resets in {reset}" if reset else "")]
        if paying
        else ["⚪ Subscription limit unavailable"]
    )
    context = context_usage_text(session)
    context_percent = session.get("context_tokens")
    window = session.get("context_window_tokens")
    if isinstance(context_percent, int) and isinstance(window, int) and window > 0:
        used = context_percent / window * 100
        context_row = f"Context [{meter(used, 7)}] {percentage(used)}"
    else:
        context_row = f"Context {context}"
    quota_meters = hook_quota_meters(
        quota_text,
        provider,
        include_monthly=paying and provider == "codex",
        include_plan=not paying,
        include_resets=not paying,
    )
    if session.get("quota_status") == "stale" and quota_meters:
        quota_meters = f"Last known {quota_meters}"
    meter_row = f"{quota_meters}  {context_row}" if quota_meters else context_row
    rows.append(f"⏱️ {meter_row}")
    rows.append(hook_forecast_row(session))
    rows.append(dashboard_line().replace("dashboard:", "Open live dashboard"))
    return ["╭─", *(f"│ {row}" for row in rows), "╰─"]


def prompt_box_context(provider: str, session_id: str) -> str | None:
    """Serialize the usage box as UserPromptSubmit context, or None when it stays hidden.

    Whether a tool-free turn qualifies is the cadence's call, not this function's,
    so the only check left here is that there is a session to describe.
    """
    session = refreshed_session(provider, session_id)
    if not isinstance(session, dict):
        return None
    body = "\n".join(
        usage_box_lines(session, recorded_quota_usage_text(provider), provider)
    )
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": f"{PROMPT_BOX_INSTRUCTION}\n\n{body}",
            }
        }
    )


def prompt_box_suppression_context() -> str:
    """Override an earlier desktop box instruction for this one reply."""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": PROMPT_BOX_SUPPRESSION_INSTRUCTION,
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


def _last_shown_path(provider: str, session_id: str) -> Path:
    return home_dir() / "shown" / f"{provider}-{session_id}.json"


def _read_last_shown(provider: str, session_id: str) -> dict[str, object]:
    try:
        value = json.loads(_last_shown_path(provider, session_id).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _record_shown(provider: str, session_id: str, state: dict[str, object]) -> None:
    try:
        path = _last_shown_path(provider, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_private_json(path, state)
    except (OSError, ValueError):
        return


def _binding_quota_window(provider: str) -> tuple[str, float] | None:
    """The busiest window of the period this provider is watched on, with its identity.

    One period per provider: five-hour for Claude, weekly for Codex. Movement in
    any other window is invisible here.

    Codex reports one window per limit bucket, so a percentage is only comparable
    with another reading of the same bucket. The identity travels with the value
    so a later comparison can tell "the meter moved" from "we changed meters".
    """
    account = stored_provider_quotas().get(provider)
    windows = account.get("windows") if isinstance(account, dict) else None
    if not isinstance(windows, list):
        return None
    period = "weekly" if provider == "codex" else "five_hour"
    candidates: list[tuple[float, str]] = []
    for window in windows:
        if not isinstance(window, dict) or window.get("period") != period:
            continue
        used = window.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        limit_id = window.get("limit_id")
        resets_at = window.get("resets_at")
        reset_identity = resets_at if isinstance(resets_at, str) else ""
        if isinstance(resets_at, str):
            try:
                # Provider timestamps drift by fractions of a second around minute boundaries.
                reset_identity = (
                    (
                        datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
                        + timedelta(seconds=30)
                    )
                    .replace(second=0, microsecond=0)
                    .isoformat()
                )
            except ValueError:
                pass
        key = json.dumps(
            [
                provider,
                period,
                limit_id if isinstance(limit_id, str) else "default",
                # A new window instance restarts near zero, so its reset time is
                # part of its identity; without it a rollover reads as a drop.
                reset_identity,
            ]
        )
        candidates.append((float(used), key))
    if not candidates:
        return None
    used, key = max(candidates, key=lambda pair: (pair[0], pair[1]))
    return key, used


def _current_usage_percent(provider: str) -> float | None:
    """Percent of the watched limit window used, for a custom rule's context."""
    binding = _binding_quota_window(provider)
    return None if binding is None else binding[1]


def _session_tool_calls(provider: str, session_id: str) -> int:
    """Tool calls in the most recent turn, read from the collector's snapshot."""
    session = refreshed_session(provider, session_id)
    if not isinstance(session, dict):
        return 0
    value = session.get("last_task_tool_calls")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _custom_rule_allows(provider: str, session_id: str, tool_calls: int | None) -> bool:
    """Run the user's own rule, if they have written one.

    The rule lives in ~/.konvu/telemetry/custom_rule.py rather than inside the
    package, so upgrading Konvu cannot silently delete it. A missing or broken
    rule shows the box: silently suppressing output is the worse failure.
    """
    path = custom_rule_module_path()
    try:
        if not path.is_file():
            return True
        spec = importlib.util.spec_from_file_location("konvu_custom_rule", path)
        if spec is None or spec.loader is None:
            return True
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        decide = getattr(module, "should_show", None)
        if not callable(decide):
            return True
        context = {
            "provider": provider,
            "session_id": session_id,
            "tool_calls": tool_calls
            if tool_calls is not None
            else _session_tool_calls(provider, session_id),
            "rule": read_preferences()["custom_rule"],
            "session": refreshed_session(provider, session_id),
            "usage_percent": _current_usage_percent(provider),
        }
        return bool(decide(context))
    except Exception:
        return True


def should_show_usage(
    provider: str, session_id: str, tool_calls: int | None = None
) -> bool:
    """Decide whether this turn should display the usage box.

    The status line is deliberately not gated here: it is ambient and always
    reflects the present, so suppressing it would show stale numbers rather
    than fewer of them.
    """
    preference = read_preferences()
    cadence = preference["cadence"]
    if cadence == "never":
        return False
    if cadence == "custom":
        return _custom_rule_allows(provider, session_id, tool_calls)
    if cadence == "every-prompt":
        return True
    if cadence == "every-tool-call":
        # A caller that already counted this turn's tool calls passes them in.
        # The prompt hooks cannot, so the count is read from the snapshot;
        # without it this cadence would silently never fire for them.
        count = (
            tool_calls
            if tool_calls is not None
            else _session_tool_calls(provider, session_id)
        )
        return count > 0
    if cadence == "usage-jump":
        return _usage_jumped(provider, session_id, preference["jump_percent"])
    return True


# Stands in for the window identity while the provider's figures are unavailable,
# so an outage shows the box once rather than on every turn until it ends.
UNKNOWN_WINDOW = "unknown"


def _usage_jumped(provider: str, session_id: str, jump_percent: float) -> bool:
    """Whether the busiest limit has moved far enough since the last box."""
    key, current = _binding_quota_window(provider) or (UNKNOWN_WINDOW, 0.0)
    last = _read_last_shown(provider, session_id)
    previous = last.get("usage_percent")
    if (
        last.get("window") != key
        or isinstance(previous, bool)
        or not isinstance(previous, (int, float))
        or current < previous
    ):
        # A different window, a missing baseline, or a figure that went backwards
        # all mean the old baseline describes a meter we are no longer reading.
        return True
    return current - float(previous) >= jump_percent


def record_usage_shown(provider: str, session_id: str) -> None:
    """Note the usage a box actually displayed, so the next jump is measured from it.

    Callers invoke this after printing, never before: a turn that decided to show
    but then found nothing to render must not consume the jump it never surfaced.
    """
    if read_preferences()["cadence"] != "usage-jump":
        return
    key, current = _binding_quota_window(provider) or (UNKNOWN_WINDOW, 0.0)
    _record_shown(provider, session_id, {"window": key, "usage_percent": current})


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
    """Return the boxed Codex usage message at the end of a completed turn."""
    request = codex_hook_request()
    if request is None:
        print(SUPPRESS_OUTPUT)
        return
    payload, session_id = request
    turn_id = payload.get("turn_id")
    transcript = codex_hook_transcript(payload, session_id)
    # The hook invocation is authoritative: spawned CLI sessions can inherit
    # desktop transcript metadata from their parent.
    if not isinstance(turn_id, str) or transcript is None:
        print(SUPPRESS_OUTPUT)
        return
    if not should_show_usage(
        "codex", session_id, codex_turn_tool_calls(transcript, turn_id)
    ):
        print(SUPPRESS_OUTPUT)
        return
    session = refreshed_session("codex", session_id)
    if not isinstance(session, dict):
        print(SUPPRESS_OUTPUT)
        return
    lines = usage_box_lines(session, recorded_quota_usage_text("codex"), "codex")
    print(json.dumps({"systemMessage": "\n" + "\n".join(lines)}))
    record_usage_shown("codex", session_id)


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
    if should_show_usage("claude", session_id):
        context = prompt_box_context("claude", session_id)
        if context is not None:
            print(context)
            record_usage_shown("claude", session_id)
            return
    print(prompt_box_suppression_context())


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
    if should_show_usage("codex", session_id):
        context = prompt_box_context("codex", session_id)
        if context is not None:
            print(context)
            record_usage_shown("codex", session_id)
            return
    print(prompt_box_suppression_context())


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
