"""Session histories, forecasts, baselines, and alert decisions."""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
import logging
from pathlib import Path
from statistics import median
from threading import Lock, Thread
from typing import Iterator, cast

from .config import (
    ACTIVITY_CLOCK_SKEW_SECONDS,
    ALERT_FORECAST_USD,
    BASELINE_LOOKBACK_SECONDS,
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
                "usage_tokens": sum(
                    event.usage.total_tokens for event in iteration_events
                ),
                "quota_tokens": round(
                    sum(event.usage.quota_tokens for event in iteration_events), 6
                ),
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


def alert_number(value: object) -> float | None:
    """Return a real number, rejecting booleans and everything non-numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def apply_session_hot_state(sessions: list[dict[str, object]], now: float) -> None:
    """Mark currently active, fully priced sessions with a large paid forecast."""
    for session in sessions:
        forecast = alert_number(session.get("projected_next_10_tasks_usd"))
        basis = session.get("forecast_basis")
        coverage = basis.get("coverage") if isinstance(basis, dict) else None
        last_activity = parse_timestamp(session.get("last_activity_at"))
        session["notification"] = {
            "hot": (
                session.get("usage_mode") in {"api_billed", "exhausted"}
                and forecast is not None
                and forecast >= ALERT_FORECAST_USD
                and coverage in (None, "fully_priced")
                and session.get("cost_status") == "complete"
                and last_activity is not None
                and -ACTIVITY_CLOCK_SKEW_SECONDS
                <= now - last_activity
                <= LIVE_ACTIVITY_SECONDS
            )
        }


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
) -> Iterator[list[tuple[float, int]]]:
    """Read completed task curves for the provider forecast fallback."""
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
                        yield series
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
            yield series


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


def median_absolute_percentage_error(samples: list[tuple[float, float]]) -> float:
    """Return median forecast error, excluding zero-cost actual windows."""
    errors = [
        abs(predicted - actual) / actual * 100
        for predicted, actual in samples
        if actual > 0
    ]
    return float(median(errors)) if errors else 0.0


def next_ten_forecast(costs: list[float], provider: str) -> float:
    """Forecast the next ten tasks from the last ten comparable tasks."""
    del provider
    recent = costs[-FORECAST_WINDOW:]
    return sum(recent) / len(recent) * 10 if recent else 0.0


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
    """Build the provider-level fallback used by sparse session forecasts."""
    provider_forecasts: dict[str, dict[str, object]] = {}
    since = now - BASELINE_LOOKBACK_SECONDS
    for provider in ("claude", "codex"):
        provider_forecast_values = [
            next_ten_forecast([cost for cost, _ in series], provider)
            for series in historical_task_series(provider, since, prices)
            if series
        ]
        provider_forecasts[provider] = {
            "median_next_10_usd": round(float(median(provider_forecast_values)), 6)
            if provider_forecast_values
            else None,
            "sessions": len(provider_forecast_values),
        }
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "lookback_days": BASELINE_LOOKBACK_SECONDS // 86400,
        "forecasts": {
            "provider_median_next_10": provider_forecasts,
        },
    }


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
        provider_forecasts = (
            forecasts.get("provider_median_next_10")
            if isinstance(forecasts, dict)
            else None
        )
        valid = (
            isinstance(baseline, dict)
            and baseline.get("schema_version") == BASELINE_SCHEMA_VERSION
            and isinstance(forecasts, dict)
            and isinstance(provider_forecasts, dict)
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
