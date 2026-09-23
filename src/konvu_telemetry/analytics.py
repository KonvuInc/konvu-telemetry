"""Session histories, forecasts, baselines, and alert decisions."""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
from statistics import median
from threading import Lock, Thread
from typing import Iterator, Literal, cast

from .config import (
    ACTIVITY_FRESHNESS_SECONDS,
    ACTIVITY_CLOCK_SKEW_SECONDS,
    ALERT_FORECAST_RENOTIFY_SECONDS,
    ALERT_FORECAST_USD,
    ALERT_QUOTA_5H_PERCENT,
    ALERT_QUOTA_RENOTIFY_SECONDS,
    ALERT_QUOTA_WEEKLY_PERCENT,
    BASELINE_LOOKBACK_SECONDS,
    BASELINE_MIN_SESSIONS,
    BASELINE_MILESTONES,
    BASELINE_REFRESH_SECONDS,
    BASELINE_SCHEMA_VERSION,
    FORECAST_WINDOW,
    FORECAST_MIN_SAMPLES,
    LIVE_ACTIVITY_SECONDS,
)
from .models import UsageEvent
from .parsers import (
    codex_events_in_file,
    codex_session_id,
    codex_subagent_parent,
    codex_task_starts,
    events_in_file,
    user_prompt_times_by_session,
)
from .pricing import cost_status, event_cost, load_pricing, requires_pricing
from .storage import (
    baseline_path,
    claude_roots,
    codex_roots,
    file_cached,
    notification_state_path,
    parse_timestamp,
    root_claude_transcripts,
    transcript_files,
    write_private_json,
)

LOGGER = logging.getLogger(__name__)
_BASELINE_REFRESH_LOCK = Lock()


def iteration_series(
    starts: list[float],
    costs: list[float | None],
    events: list[UsageEvent],
    priced: bool | list[bool] = True,
) -> list[dict[str, object]]:
    """Build the cumulative, per-iteration usage history used by the dashboard."""
    main_events = sorted(
        (event for event in events if not event.is_subagent),
        key=lambda event: event.timestamp,
    )
    cumulative_cost = 0.0
    rows: list[dict[str, object]] = []
    for index, (start, cost) in enumerate(zip(starts, costs), start=1):
        iteration_priced = priced[index - 1] if isinstance(priced, list) else priced
        end = starts[index] if index < len(starts) else float("inf")
        iteration_events = [
            event for event in main_events if start <= event.timestamp < end
        ]
        prior_events = [event for event in main_events if event.timestamp < end]
        context_event = (
            iteration_events[-1]
            if iteration_events
            else (prior_events[-1] if prior_events else None)
        )
        if isinstance(cost, (int, float)):
            cumulative_cost += cost
        configuration = single_configuration(iteration_events)
        model, effort, speed = (
            configuration if configuration is not None else (None, None, None)
        )
        rows.append(
            {
                "iteration": index,
                "started_at": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                "cost_usd": round(cost, 6) if isinstance(cost, (int, float)) else None,
                "cumulative_cost_usd": round(cumulative_cost, 6),
                "priced": iteration_priced,
                "context_tokens": context_event.usage.context_tokens
                if context_event
                else 0,
                "tool_calls": sum(event.tool_calls for event in iteration_events),
                "model": model,
                "reasoning_effort": effort,
                "speed": speed,
            }
        )
    return rows


def context_usage_history(
    starts: list[float],
    events: list[UsageEvent],
    prices: dict[str, dict[str, float]],
    attribution_times: dict[int, float] | None = None,
) -> list[dict[str, object]]:
    """Record the context observed with each local usage checkpoint and spend to that point."""
    cumulative_cost = 0.0
    context_tokens: int | None = None
    observed_at: str | None = None
    history: list[dict[str, object]] = []
    attribution_times = attribution_times or {}
    for event in sorted(
        events, key=lambda item: attribution_times.get(id(item), item.timestamp)
    ):
        attributed_at = attribution_times.get(id(event), event.timestamp)
        cost = event_cost(event, prices)
        if cost is not None:
            cumulative_cost += cost
        if not event.is_subagent:
            context_tokens = event.usage.context_tokens
            observed_at = datetime.fromtimestamp(
                event.timestamp, timezone.utc
            ).isoformat()
        if context_tokens is not None:
            history.append(
                {
                    "timestamp": datetime.fromtimestamp(
                        attributed_at, timezone.utc
                    ).isoformat(),
                    "cumulative_cost_usd": round(cumulative_cost, 6),
                    "context_tokens": context_tokens,
                    "context_observed_at": observed_at,
                    "iteration": bisect_right(starts, attributed_at),
                }
            )
    return history


def locate_compactions(snapshot: dict[str, object]) -> None:
    """Attach each explicit compaction to the spend and prompt recorded at its event time."""
    sessions = snapshot.get("sessions")
    for session in sessions if isinstance(sessions, list) else []:
        if not isinstance(session, dict):
            continue
        raw_history = session.get("context_history")
        history = (
            [row for row in raw_history if isinstance(row, dict)]
            if isinstance(raw_history, list)
            else []
        )
        raw_events = session.get("compact_events")
        events = (
            [row for row in raw_events if isinstance(row, dict)]
            if isinstance(raw_events, list)
            else []
        )
        if not history or not events:
            continue
        times = [parse_timestamp(row.get("timestamp")) or 0.0 for row in history]
        located: list[dict[str, object]] = []
        for event in events:
            timestamp = parse_timestamp(event.get("timestamp"))
            if timestamp is None:
                continue
            index = bisect_right(times, timestamp) - 1
            before = history[index] if index >= 0 else None
            located.append(
                {
                    **event,
                    "cumulative_cost_usd": before.get("cumulative_cost_usd")
                    if before
                    else 0.0,
                    "iteration": before.get("iteration") if before else 0,
                    "observed_pre_tokens": before.get("context_tokens")
                    if before
                    else None,
                    "context_observed_at": before.get("context_observed_at")
                    if before
                    else None,
                }
            )
        session["compact_events"] = located


def quota_alert_window(window: dict[str, object]) -> tuple[str, int] | None:
    """Return the alert name and threshold for a supported quota window."""
    minutes = window.get("window_minutes")
    if not isinstance(minutes, (int, float)):
        return None
    if minutes == 5 * 60:
        return "5-hour", ALERT_QUOTA_5H_PERCENT
    if minutes == 7 * 24 * 60:
        return "weekly", ALERT_QUOTA_WEEKLY_PERCENT
    return None


def alert_number(value: object) -> float | None:
    """Return a real number, rejecting booleans and everything non-numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _provider_quota_windows(
    provider: str, quotas: dict[str, object], now: float
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Return the provider's included-usage windows and those not yet reset."""
    raw_windows = quotas.get("windows")
    windows = (
        [window for window in raw_windows if isinstance(window, dict)]
        if isinstance(raw_windows, list)
        else []
    )
    supported = [
        window
        for window in windows
        if quota_alert_window(window) is not None
        and window.get("limit_id") in (None, "default", provider)
    ]
    active = []
    for window in supported:
        reset_at = parse_timestamp(window.get("resets_at"))
        if reset_at is None or reset_at > now:
            active.append(window)
    return supported, active


def _provider_notification_mode(
    provider: str,
    quotas: dict[str, object] | None,
    previous: dict[str, object],
    now: float,
) -> tuple[str, dict[str, object]]:
    """Choose quota or money alerts from current provider state and prior reset timing."""
    observed_at = None
    malformed_quotas = False
    if quotas is not None:
        observed_at = parse_timestamp(quotas.get("observed_at"))
        previous_observed_at = parse_timestamp(previous.get("observed_at"))
        if observed_at is None or observed_at - now > ACTIVITY_CLOCK_SKEW_SECONDS:
            quotas = None
            malformed_quotas = True
        elif now - observed_at > ACTIVITY_FRESHNESS_SECONDS or (
            previous_observed_at is not None and observed_at < previous_observed_at
        ):
            quotas = None
    if quotas is not None and observed_at is not None:
        supported, active = _provider_quota_windows(provider, quotas, now)
        reached = quotas.get("rate_limit_reached_type") == "rate_limit_reached"
        if any(
            alert_number(window.get("used_percent")) is None for window in supported
        ):
            return "money", {
                "mode": "money",
                "observed_at": quotas["observed_at"],
            }
        reached_candidates = [
            window
            for window in supported
            if (reset := parse_timestamp(window.get("resets_at"))) is None
            or reset >= observed_at
        ]
        reached_window = (
            max(
                reached_candidates,
                key=lambda window: alert_number(window.get("used_percent")) or 0,
            )
            if reached_candidates
            else None
        )
        reached_reset = (
            parse_timestamp(reached_window.get("resets_at"))
            if reached_window is not None
            else None
        )
        if reached_reset is not None and reached_reset <= now:
            reached = False
        if supported or reached:
            exhausted = [
                window
                for window in active
                if (alert_number(window.get("used_percent")) or 0) >= 100
            ]
            mode = "money" if reached or exhausted else "quota"
            record: dict[str, object] = {
                "mode": mode,
                "last_quota_seen_at": now,
            }
            reset_times = [
                reset
                for window in exhausted
                if (reset := parse_timestamp(window.get("resets_at"))) is not None
            ]
            if reached and not reset_times and reached_reset is not None:
                reset_times = [reached_reset]
            if reset_times:
                record["money_until"] = max(reset_times)
            record["observed_at"] = quotas["observed_at"]
            for key in (
                "plan_type",
                "rate_limit_reached_type",
                "spend_control_reached",
                "has_credits",
                "credits_unlimited",
            ):
                if key in quotas:
                    record[key] = quotas[key]
            return mode, record
        return "money", {
            "mode": "money",
            "observed_at": quotas["observed_at"],
        }
    if malformed_quotas:
        return "money", {"mode": "money"}
    previous_mode = previous.get("mode")
    money_until = alert_number(previous.get("money_until"))
    if previous_mode == "money" and money_until is not None and now >= money_until:
        return "quota", {"mode": "quota", "last_quota_seen_at": now}
    last_quota_seen_at = alert_number(previous.get("last_quota_seen_at"))
    if (
        previous_mode == "quota"
        and last_quota_seen_at is not None
        and now - last_quota_seen_at > ACTIVITY_FRESHNESS_SECONDS
    ):
        return "money", {"mode": "money"}
    if previous_mode in ("quota", "money"):
        return str(previous_mode), dict(previous)
    return "money", {"mode": "money"}


def apply_notification_tracking(
    sessions: list[dict[str, object]],
    now: float,
    account_quotas: dict[str, object] | None = None,
) -> None:
    """Persist alerts so repeated browser polls do not repeatedly notify the user."""
    try:
        raw_state = json.loads(notification_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        raw_state = {}
    state = raw_state if isinstance(raw_state, dict) else {}
    quota_map = account_quotas or {}
    providers = {provider for provider in quota_map if isinstance(provider, str)}
    for session in sessions:
        provider = session.get("provider")
        if isinstance(provider, str):
            providers.add(provider)
    provider_modes: dict[str, str] = {}
    for provider in providers:
        key = f"provider-mode:{provider}"
        previous = state.get(key)
        previous_record = previous if isinstance(previous, dict) else {}
        raw_quotas = quota_map.get(provider)
        provider_quotas = raw_quotas if isinstance(raw_quotas, dict) else None
        mode, record = _provider_notification_mode(
            provider, provider_quotas, previous_record, now
        )
        provider_modes[provider] = mode
        state[key] = record
    for session in sessions:
        session_id = session.get("id")
        provider = session.get("provider")
        comparisons = session.get("baselines")
        comparison = (
            comparisons.get("provider") if isinstance(comparisons, dict) else None
        )
        if not isinstance(session_id, str) or not isinstance(provider, str):
            continue
        key = f"{provider}:{session_id}"
        previous = state.get(key)
        record = previous if isinstance(previous, dict) else {}
        overhead = (
            comparison.get("cost_overhead_percent")
            if isinstance(comparison, dict)
            else None
        )
        forecast = alert_number(session.get("projected_next_10_tasks_usd"))
        basis = session.get("forecast_basis")
        coverage = basis.get("coverage") if isinstance(basis, dict) else None
        last_activity = parse_timestamp(session.get("last_activity_at"))
        if (
            provider_modes.get(provider) == "quota"
            or forecast is None
            # A forecast borrowed from a median is display-only; the session has not earned it.
            or coverage not in (None, "fully_priced")
            or session.get("cost_status") != "complete"
            or forecast <= ALERT_FORECAST_USD
            or last_activity is None
            # Bounded below too: a skewed future stamp must not pin a dead session live.
            or not -ACTIVITY_CLOCK_SKEW_SECONDS
            <= now - last_activity
            <= LIVE_ACTIVITY_SECONDS
        ):
            # Forget the alerted peak, but keep the clock: the repeat floor spans cooldowns.
            cooled = alert_number(record.get("last_notified_at"))
            record = {
                "sequence": int(alert_number(record.get("sequence")) or 0),
                "hot": False,
            }
            if cooled is not None:
                record["last_notified_at"] = cooled
            state[key] = record
            session["notification"] = dict(record)
            continue
        sequence = int(alert_number(record.get("sequence")) or 0)
        last_notified_at = alert_number(record.get("last_notified_at"))
        last_forecast = alert_number(record.get("last_forecast_usd"))
        if last_notified_at is None:
            notify = True
        else:
            # A forgotten peak re-arms the comparison but never skips the repeat floor.
            notify = now - last_notified_at >= ALERT_FORECAST_RENOTIFY_SECONDS and (
                last_forecast is None or forecast >= last_forecast
            )
        if notify:
            sequence += 1
            last_notified_at = now
            last_forecast = forecast
        record = {
            "sequence": sequence,
            "hot": True,
            "last_notified_at": last_notified_at,
            "last_forecast_usd": last_forecast,
        }
        state[key] = record
        # Display-only: the median no longer gates the alert.
        session["notification"] = {**record, "overhead_percent": overhead}
    for provider, raw_provider_quotas in quota_map.items():
        if not isinstance(provider, str) or not isinstance(raw_provider_quotas, dict):
            continue
        provider_quotas = raw_provider_quotas
        notifications: list[dict[str, object]] = []
        quota_key = f"quota:{provider}"
        previous = state.get(quota_key)
        record = previous if isinstance(previous, dict) else {}
        if provider_modes.get(provider) != "quota":
            state[quota_key] = {**record, "hot": False}
            provider_quotas["notifications"] = notifications
            continue
        _, windows = _provider_quota_windows(provider, provider_quotas, now)
        candidates: list[tuple[float, dict[str, object], str]] = []
        for window in windows:
            alert_window = quota_alert_window(window)
            used_percent = alert_number(window.get("used_percent"))
            if alert_window is None or used_percent is None:
                continue
            window_name, threshold = alert_window
            if used_percent >= threshold:
                candidates.append((used_percent, window, window_name))
        if not candidates:
            state[quota_key] = {**record, "hot": False}
            provider_quotas["notifications"] = notifications
            continue
        used_percent, window, window_name = max(
            candidates,
            key=lambda item: (
                item[0],
                -(alert_number(item[1].get("window_minutes")) or 0),
            ),
        )
        reset_at = window.get("resets_at")
        reset_changed = isinstance(reset_at, str) and reset_at != record.get("reset_at")
        last_notified_at = alert_number(record.get("last_notified_at"))
        last_used_percent = alert_number(record.get("last_used_percent"))
        first_alert = record.get("hot") is not True or reset_changed
        rising = last_used_percent is not None and used_percent > last_used_percent
        may_renotify = (
            last_notified_at is not None
            and now - last_notified_at >= ALERT_QUOTA_RENOTIFY_SECONDS
        )
        if first_alert or (may_renotify and rising):
            record["sequence"] = int(alert_number(record.get("sequence")) or 0) + 1
            record["last_notified_at"] = now
            record["last_used_percent"] = used_percent
        record["hot"] = True
        record["window"] = window_name
        if isinstance(reset_at, str):
            record["reset_at"] = reset_at
        state[quota_key] = record
        source_session_id = window.get("session_id")
        notification: dict[str, object] = {
            "sequence": int(alert_number(record.get("sequence")) or 0),
            "hot": True,
            "window": window_name,
            "used_percent": round(used_percent),
        }
        if isinstance(source_session_id, str):
            notification["session_id"] = source_session_id
        notifications.append(notification)
        provider_quotas["notifications"] = notifications
    write_private_json(notification_state_path(), state)


def task_series(
    events: list[UsageEvent], starts: list[float], prices: dict[str, dict[str, float]]
) -> list[tuple[float, int]] | None:
    """Turn per-call accounting into cumulative cost and token checkpoints per task."""
    totals = [[0.0, 0.0] for _ in starts]
    for event in events:
        index = bisect_right(starts, event.timestamp) - 1
        if index < 0:
            continue
        cost = event_cost(event, prices)
        if cost is None and requires_pricing(event):
            return None
        if cost is None:
            continue
        totals[index][0] += cost
        totals[index][1] += event.usage.total_tokens
    return [(values[0], int(values[1])) for values in totals]


def single_configuration(events: list[UsageEvent]) -> tuple[str, str, str] | None:
    """Return one priced model, effort, and speed when a task series is uniform."""
    configurations = {
        (event.model, event.effort, event.usage.speed)
        for event in events
        if requires_pricing(event)
    }
    return next(iter(configurations)) if len(configurations) == 1 else None


def single_model_effort(events: list[UsageEvent]) -> tuple[str, str] | None:
    """Return one priced model and effort when a task series is uniform."""
    configurations = {
        (event.model, event.effort) for event in events if requires_pricing(event)
    }
    return next(iter(configurations)) if len(configurations) == 1 else None


def deduplicate_usage_events(events: list[UsageEvent]) -> list[UsageEvent]:
    """Keep one event per message, preferring an explicitly marked subagent copy."""
    deduplicated: dict[str, UsageEvent] = {}
    anonymous: list[UsageEvent] = []
    for event in events:
        if event.message_id is None:
            anonymous.append(event)
            continue
        previous = deduplicated.get(event.message_id)
        if previous is None or (event.is_subagent and not previous.is_subagent):
            deduplicated[event.message_id] = event
    return sorted(
        [*deduplicated.values(), *anonymous], key=lambda event: event.timestamp
    )


def historical_task_series(
    provider: str, since: float, prices: dict[str, dict[str, float]]
) -> Iterator[tuple[str | None, str | None, list[tuple[float, int]]]]:
    """Read completed task curves and trustworthy model-effort cohorts."""
    if provider == "claude":
        for root in claude_roots():
            if not root.is_dir():
                continue
            for transcript in root_claude_transcripts(root):
                try:
                    if transcript.stat().st_mtime < since:
                        continue
                except OSError:
                    continue
                session_id = transcript.stem
                starts = user_prompt_times_by_session(transcript).get(session_id, [])
                root_events = [
                    event
                    for event in events_in_file(transcript)
                    if event.session_id == session_id
                ]
                main_events = [event for event in root_events if not event.is_subagent]
                child_events = [
                    event
                    for path in transcript_files(transcript.parent / session_id)
                    for event in events_in_file(path)
                    if event.session_id == session_id and event.is_subagent
                ]
                events = deduplicate_usage_events([*root_events, *child_events])
                if starts and events and cost_status(events, prices)[0] == "complete":
                    series = task_series(events, starts, prices)
                    if series is not None:
                        configuration = single_model_effort(main_events)
                        model, effort = (
                            configuration if configuration is not None else (None, None)
                        )
                        yield model, effort, series
        return
    roots: list[tuple[Path, list[UsageEvent]]] = []
    children: dict[str, list[UsageEvent]] = defaultdict(list)
    parents: dict[str, str] = {}
    entries: list[tuple[Path, list[UsageEvent], tuple[str, str] | None]] = []
    for root in codex_roots():
        if not root.is_dir():
            continue
        for transcript in transcript_files(root):
            try:
                if transcript.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            entries.append(
                (
                    transcript,
                    list(codex_events_in_file(transcript)),
                    codex_subagent_parent(transcript),
                )
            )
    for transcript, events, parent in entries:
        session_id = codex_session_id(transcript)
        if parent is not None:
            parents[session_id] = parent[0]
        else:
            roots.append((transcript, events))

    def root_session_id(session_id: str) -> str:
        visited: set[str] = set()
        while session_id in parents and session_id not in visited:
            visited.add(session_id)
            session_id = parents[session_id]
        return session_id

    for transcript, events, parent in entries:
        if parent is not None:
            children[root_session_id(codex_session_id(transcript))].extend(events)
    for transcript, main_events in roots:
        starts = codex_task_starts(transcript)
        events = deduplicate_usage_events(
            [*main_events, *children[codex_session_id(transcript)]]
        )
        if (
            not starts
            or not main_events
            or cost_status(events, prices)[0] != "complete"
        ):
            continue
        series = task_series(events, starts, prices)
        if series is not None:
            configuration = single_model_effort(main_events)
            model, effort = configuration if configuration is not None else (None, None)
            yield model, effort, series


@file_cached
def claude_compact_times(file_path: Path) -> list[float]:
    """Return timestamps where Claude recorded a completed context compaction."""
    times: list[float] = []
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(record, dict)
                    and record.get("type") == "system"
                    and record.get("subtype") == "compact_boundary"
                ):
                    timestamp = parse_timestamp(record.get("timestamp"))
                    if timestamp is not None:
                        times.append(timestamp)
    except OSError:
        return []
    return times


def claude_task_costs(
    file_path: Path, prices: dict[str, dict[str, float]]
) -> tuple[list[float], list[float]] | None:
    """Return root prompt boundaries and their fully attributed local costs."""
    session_id = file_path.stem
    starts = user_prompt_times_by_session(file_path).get(session_id, [])
    costs = [0.0] * len(starts)
    for event in events_in_file(file_path):
        if event.session_id != session_id or event.is_subagent:
            continue
        cost = event_cost(event, prices)
        if cost is None and requires_pricing(event):
            return None
        index = bisect_right(starts, event.timestamp) - 1
        if cost is not None and index >= 0:
            costs[index] += cost
    return starts, costs


def claude_compact_next_ten_costs(
    since: float, prices: dict[str, dict[str, float]]
) -> list[float]:
    """Collect completed ten-task windows that began immediately after compact."""
    windows: list[float] = []
    for root in claude_roots():
        if not root.is_dir():
            continue
        for transcript in root_claude_transcripts(root):
            try:
                if transcript.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            task_costs = claude_task_costs(transcript, prices)
            if task_costs is None:
                continue
            starts, costs = task_costs
            for compact_time in claude_compact_times(transcript):
                point = bisect_right(starts, compact_time)
                if point + 10 <= len(costs):
                    windows.append(sum(costs[point : point + 10]))
    return windows


def median_absolute_percentage_error(samples: list[tuple[float, float]]) -> float:
    """Return median forecast error, excluding zero-cost actual windows."""
    errors = [
        abs(predicted - actual) / actual * 100
        for predicted, actual in samples
        if actual > 0
    ]
    return float(median(errors)) if errors else 0.0


def configuration_key(model: str, effort: str) -> str:
    """Build a stable model and effort baseline key."""
    return json.dumps([model, effort], separators=(",", ":"))


def next_ten_forecast(costs: list[float], provider: str) -> float:
    """Forecast the next ten tasks from the last ten comparable tasks."""
    del provider
    recent = costs[-FORECAST_WINDOW:]
    return sum(recent) / len(recent) * 10 if recent else 0.0


def forecast_backtest_sample(
    series: list[tuple[float, int]],
) -> tuple[float, float] | None:
    """Backtest the live forecast against a held-out final ten-task window."""
    if len(series) < FORECAST_WINDOW * 2:
        return None
    costs = [cost for cost, _ in series]
    point = len(costs) - FORECAST_WINDOW
    prediction = next_ten_forecast(costs[:point], "historical")
    return prediction, sum(costs[point:])


def scaled_precompact_forecast(
    costs: list[float], current_context: int, precompact_context: int
) -> float | None:
    """Scale the pre-compact trend until enough post-compact prompts exist."""
    if (
        current_context <= 0
        or precompact_context <= 0
        or len(costs) < FORECAST_MIN_SAMPLES
    ):
        return None
    forecast = next_ten_forecast(costs, "claude")
    if forecast <= 0:
        return None
    return forecast * min(1.0, current_context / precompact_context)


def backtest_next_ten() -> None:
    """Backtest the rolling next-ten forecast over 50 recorded Claude sessions."""
    prices = load_pricing()
    transcripts = sorted(
        (
            path
            for root in claude_roots()
            if root.is_dir()
            for path in root_claude_transcripts(root)
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    regular: list[tuple[float, float]] = []
    compact: list[tuple[float, float]] = []
    selected = 0
    for transcript in transcripts:
        task_costs = claude_task_costs(transcript, prices)
        if task_costs is None:
            continue
        starts, costs = task_costs
        if len(costs) < 20:
            continue
        point = len(costs) - 10
        prediction = sum(costs[max(0, point - 10) : point]) / min(10, point) * 10
        actual = sum(costs[point : point + 10])
        regular.append((prediction, actual))
        selected += 1
        compact_points = [
            bisect_right(starts, timestamp)
            for timestamp in claude_compact_times(transcript)
        ]
        for compact_point in compact_points:
            if compact_point < 1 or compact_point + 10 > len(costs):
                continue
            post_compact_history = costs[max(0, compact_point - 3) : compact_point]
            reset_prediction = (
                sum(post_compact_history) / len(post_compact_history) * 10
            )
            compact.append(
                (reset_prediction, sum(costs[compact_point : compact_point + 10]))
            )
        if selected == 50:
            break
    result = {
        "sessions": selected,
        "rolling_10": {
            "median_actual_usd": round(
                float(median(actual for _, actual in regular)), 4
            )
            if regular
            else 0,
            "median_predicted_usd": round(
                float(median(predicted for predicted, _ in regular)), 4
            )
            if regular
            else 0,
            "median_absolute_error_percent": round(
                median_absolute_percentage_error(regular), 1
            ),
        },
        "after_compact": {
            "windows": len(compact),
            "median_actual_usd": round(
                float(median(actual for _, actual in compact)), 4
            )
            if compact
            else 0,
            "median_predicted_usd": round(
                float(median(predicted for predicted, _ in compact)), 4
            )
            if compact
            else 0,
            "median_absolute_error_percent": round(
                median_absolute_percentage_error(compact), 1
            ),
            "compact_baseline_prediction_usd": round(
                float(median(actual for _, actual in compact)), 4
            )
            if compact
            else 0,
            "compact_baseline_median_absolute_error_percent": round(
                median_absolute_percentage_error(
                    [
                        (
                            float(
                                median(
                                    [
                                        other_actual
                                        for other_index, (_, other_actual) in enumerate(
                                            compact
                                        )
                                        if other_index != index
                                    ]
                                )
                            ),
                            actual,
                        )
                        for index, (_, actual) in enumerate(compact)
                    ]
                )
                if len(compact) > 1
                else 0,
                1,
            ),
        },
    }
    print(json.dumps(result, indent=2))


def build_baselines(
    now: float, prices: dict[str, dict[str, float]]
) -> dict[str, object]:
    """Build provider and model-effort median checkpoints."""
    providers: dict[str, list[dict[str, object]]] = {}
    configurations: dict[str, dict[str, dict[str, object]]] = {}
    forecast_backtests: dict[str, dict[str, object]] = {}
    provider_forecasts: dict[str, dict[str, object]] = {}
    since = now - BASELINE_LOOKBACK_SECONDS
    for provider in ("claude", "codex"):
        provider_series: list[list[tuple[float, int]]] = []
        configuration_series: dict[str, list[list[tuple[float, int]]]] = defaultdict(
            list
        )
        configuration_labels: dict[str, tuple[str, str]] = {}
        forecast_samples: list[tuple[float, float]] = []
        for model, effort, series in historical_task_series(provider, since, prices):
            provider_series.append(series)
            forecast_sample = forecast_backtest_sample(series)
            if forecast_sample is not None:
                forecast_samples.append(forecast_sample)
            if model is not None and effort is not None:
                key = configuration_key(model, effort)
                configuration_labels[key] = (model, effort)
                configuration_series[key].append(series)
        providers[provider] = cumulative_median_checkpoints(provider_series)
        provider_forecast_values = [
            next_ten_forecast([cost for cost, _ in series], provider)
            for series in provider_series
            if series
        ]
        provider_forecasts[provider] = {
            "median_next_10_usd": round(float(median(provider_forecast_values)), 6)
            if provider_forecast_values
            else None,
            "sessions": len(provider_forecast_values),
        }
        valid_forecast_samples = [
            sample for sample in forecast_samples if sample[1] > 0
        ]
        forecast_backtests[provider] = {
            "samples": len(valid_forecast_samples),
            "median_absolute_percentage_error": round(
                median_absolute_percentage_error(valid_forecast_samples), 1
            )
            if valid_forecast_samples
            else None,
        }
        configurations[provider] = {}
        for key, cohort_series in configuration_series.items():
            model, effort = configuration_labels[key]
            config_checkpoints = cumulative_median_checkpoints(cohort_series)
            if config_checkpoints:
                configurations[provider][key] = {
                    "model": model,
                    "effort": effort,
                    "checkpoints": config_checkpoints,
                }
    compact_windows = claude_compact_next_ten_costs(since, prices)
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "lookback_days": BASELINE_LOOKBACK_SECONDS // 86400,
        "minimum_sessions": BASELINE_MIN_SESSIONS,
        "milestones": list(BASELINE_MILESTONES),
        "median_method": "checkpoint_cohort_medians",
        "providers": providers,
        "configurations": configurations,
        "forecasts": {
            "claude_after_compact_next_10_usd": round(float(median(compact_windows)), 6)
            if compact_windows
            else 0.0,
            "claude_after_compact_samples": len(compact_windows),
            "provider_median_next_10": provider_forecasts,
            "rolling_next_10_backtest": forecast_backtests,
        },
    }


def cumulative_median_checkpoints(
    series: list[list[tuple[float, int]]],
) -> list[dict[str, object]]:
    """Return cumulative sums of per-prompt medians for reached sessions."""
    milestones = set(BASELINE_MILESTONES)
    observed_iterations = max((len(row) for row in series), default=0)
    cumulative_cost = 0.0
    cumulative_tokens = 0.0
    checkpoints: list[dict[str, object]] = []
    for iteration in range(1, observed_iterations + 1):
        cohort = [row for row in series if len(row) >= iteration]
        if len(cohort) < BASELINE_MIN_SESSIONS:
            break
        cumulative_cost += float(median(row[iteration - 1][0] for row in cohort))
        cumulative_tokens += float(median(row[iteration - 1][1] for row in cohort))
        if iteration not in milestones:
            continue
        checkpoints.append(
            {
                "iterations": iteration,
                "sessions": len(cohort),
                "median_cost_usd": round(cumulative_cost, 6),
                "median_tokens": int(cumulative_tokens),
            }
        )
    return checkpoints


def load_baselines(
    now: float,
    prices: dict[str, dict[str, float]],
    refresh_in_background: bool = False,
) -> dict[str, object]:
    """Reuse a recent baseline so the minute collector only reads active sessions."""
    try:
        baseline = cast(dict[str, object], json.loads(baseline_path().read_text()))
        generated_at = (
            parse_timestamp(baseline.get("generated_at"))
            if isinstance(baseline, dict)
            else None
        )
        forecasts = baseline.get("forecasts") if isinstance(baseline, dict) else None
        configurations = (
            baseline.get("configurations") if isinstance(baseline, dict) else None
        )
        valid = (
            isinstance(baseline, dict)
            and baseline.get("schema_version") == BASELINE_SCHEMA_VERSION
            and baseline.get("milestones") == list(BASELINE_MILESTONES)
            and baseline.get("minimum_sessions") == BASELINE_MIN_SESSIONS
            and baseline.get("median_method") == "checkpoint_cohort_medians"
            and isinstance(forecasts, dict)
            and isinstance(configurations, dict)
            and generated_at is not None
        )
        if valid and generated_at is not None:
            if now - generated_at < BASELINE_REFRESH_SECONDS:
                return baseline
            if refresh_in_background:
                if _BASELINE_REFRESH_LOCK.acquire(blocking=False):
                    try:
                        Thread(
                            target=_refresh_baselines,
                            args=(now, prices),
                            daemon=True,
                        ).start()
                    except RuntimeError:
                        _BASELINE_REFRESH_LOCK.release()
                        LOGGER.exception("Could not start background baseline refresh")
                return baseline
    except (OSError, json.JSONDecodeError):
        pass
    baseline = build_baselines(now, prices)
    destination = baseline_path()
    write_private_json(destination, baseline)
    return baseline


def _refresh_baselines(now: float, prices: dict[str, dict[str, float]]) -> None:
    try:
        write_private_json(baseline_path(), build_baselines(now, prices))
    except Exception:
        LOGGER.exception("Background baseline refresh failed")
    finally:
        _BASELINE_REFRESH_LOCK.release()


def baseline_comparison(
    provider: str,
    task_count: int,
    token_count: int,
    baseline: dict[str, object],
    model: str = "unknown",
    effort: str = "standard",
    since_compact: bool = False,
    cost_usd: float | None = None,
    comparison_scope: Literal["provider", "model_effort"] = "provider",
) -> dict[str, object] | None:
    def eligible_checkpoints(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        eligible: list[dict[str, object]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            iterations = item.get("iterations")
            sessions = item.get("sessions")
            if (
                not isinstance(iterations, int)
                or isinstance(iterations, bool)
                or not isinstance(sessions, int)
                or isinstance(sessions, bool)
                or sessions < BASELINE_MIN_SESSIONS
            ):
                continue
            eligible.append(item)
        return eligible

    def checkpoint_iteration(item: dict[str, object]) -> int:
        iteration = item.get("iterations")
        if not isinstance(iteration, int):
            raise ValueError("eligible checkpoint has no iteration")
        return iteration

    def covers(items: list[dict[str, object]]) -> bool:
        iterations = [checkpoint_iteration(item) for item in items]
        return bool(iterations) and min(iterations) <= task_count <= max(iterations)

    providers = baseline.get("providers")
    provider_checkpoints = eligible_checkpoints(
        providers.get(provider) if isinstance(providers, dict) else None
    )
    scope = "provider"
    configurations = baseline.get("configurations")
    provider_configurations = (
        configurations.get(provider) if isinstance(configurations, dict) else None
    )
    configuration = (
        provider_configurations.get(configuration_key(model, effort))
        if isinstance(provider_configurations, dict)
        else None
    )
    config_checkpoints = (
        configuration.get("checkpoints") if isinstance(configuration, dict) else None
    )
    configuration_checkpoints = eligible_checkpoints(config_checkpoints)
    if comparison_scope == "model_effort":
        eligible = configuration_checkpoints
        scope = "model_effort"
    else:
        eligible = provider_checkpoints
    if not covers(eligible):
        return None
    ordered = sorted(eligible, key=checkpoint_iteration)
    lower_index = (
        bisect_right([checkpoint_iteration(item) for item in ordered], task_count) - 1
    )
    lower = ordered[lower_index]
    upper = ordered[lower_index + 1] if lower_index + 1 < len(ordered) else None
    if task_count == checkpoint_iteration(lower):
        upper = lower
    if upper is None:
        return None

    lower_iterations = checkpoint_iteration(lower)
    lower_tokens = lower.get("median_tokens")
    upper_iterations = checkpoint_iteration(upper)
    upper_tokens = upper.get("median_tokens")
    if (
        not isinstance(lower_tokens, int)
        or lower_tokens < 0
        or not isinstance(upper_tokens, int)
        or upper_tokens <= 0
        or (upper_iterations <= lower_iterations and task_count != lower_iterations)
    ):
        return None
    fraction = (
        0
        if upper_iterations == lower_iterations
        else (task_count - lower_iterations) / (upper_iterations - lower_iterations)
    )
    typical_tokens = round(lower_tokens + (upper_tokens - lower_tokens) * fraction)
    if typical_tokens <= 0:
        return None
    lower_cost = lower.get("median_cost_usd")
    upper_cost = upper.get("median_cost_usd")
    typical_cost = (
        round(float(lower_cost) + (float(upper_cost) - float(lower_cost)) * fraction, 6)
        if isinstance(lower_cost, (int, float)) and isinstance(upper_cost, (int, float))
        else None
    )
    lower_sessions = lower.get("sessions", 0)
    upper_sessions = upper.get("sessions", 0)
    sample_sessions = (
        min(lower_sessions, upper_sessions)
        if isinstance(lower_sessions, int) and isinstance(upper_sessions, int)
        else 0
    )
    overhead_percent = round((token_count / typical_tokens - 1) * 100)
    cost_overhead_percent = (
        round((cost_usd / typical_cost - 1) * 100)
        if isinstance(cost_usd, (int, float))
        and isinstance(typical_cost, (int, float))
        and typical_cost > 0
        else None
    )
    comparison_percent = (
        cost_overhead_percent if cost_overhead_percent is not None else overhead_percent
    )
    emoji = (
        "🟢" if comparison_percent <= 10 else "🟠" if comparison_percent <= 50 else "🔴"
    )
    return {
        "iterations": task_count,
        "sample_sessions": sample_sessions,
        "median_cost_usd": typical_cost,
        "median_tokens": typical_tokens,
        "token_overhead_percent": overhead_percent,
        "cost_overhead_percent": cost_overhead_percent,
        "emoji": emoji,
        "since_compact": since_compact,
        "scope": scope,
        "model": model,
        "effort": effort,
    }
