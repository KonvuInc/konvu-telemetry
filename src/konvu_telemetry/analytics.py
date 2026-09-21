"""Session histories, forecasts, baselines, and alert decisions."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median
from typing import Iterator, Literal

from .config import (
    ALERT_FORECAST_USD,
    ALERT_OVERHEAD_PERCENT,
    ALERT_RENOTIFY_SECONDS,
    BASELINE_LOOKBACK_SECONDS,
    BASELINE_MILESTONES,
    BASELINE_REFRESH_SECONDS,
    BASELINE_SCHEMA_VERSION,
    FORECAST_WINDOW,
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


def iteration_series(
    starts: list[float],
    costs: list[float],
    events: list[UsageEvent],
    priced: bool = True,
) -> list[dict[str, object]]:
    """Build the cumulative, per-iteration usage history used by the dashboard."""
    main_events = sorted(
        (event for event in events if not event.is_subagent),
        key=lambda event: event.timestamp,
    )
    cumulative_cost = 0.0
    rows: list[dict[str, object]] = []
    for index, (start, cost) in enumerate(zip(starts, costs), start=1):
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
        cumulative_cost += cost
        rows.append(
            {
                "iteration": index,
                "started_at": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                "cost_usd": round(cost, 6),
                "cumulative_cost_usd": round(cumulative_cost, 6),
                "priced": priced,
                "context_tokens": context_event.usage.context_tokens
                if context_event
                else 0,
                "tool_calls": sum(event.tool_calls for event in iteration_events),
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


def apply_notification_tracking(sessions: list[dict[str, object]], now: float) -> None:
    """Persist alerts so repeated browser polls do not repeatedly notify the user."""
    try:
        raw_state = json.loads(notification_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        raw_state = {}
    state = raw_state if isinstance(raw_state, dict) else {}
    for session in sessions:
        session_id = session.get("id")
        provider = session.get("provider")
        comparison = session.get("baseline")
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
        cost = session.get("total_cost_usd")
        forecast = session.get("projected_next_10_tasks_usd")
        cost_status_value = session.get("cost_status")
        if (
            not isinstance(overhead, (int, float))
            or not isinstance(cost, (int, float))
            or not isinstance(forecast, (int, float))
            or cost_status_value != "complete"
            or overhead < ALERT_OVERHEAD_PERCENT
            or forecast <= ALERT_FORECAST_USD
        ):
            record["hot"] = False
            state[key] = record
            session["notification"] = {
                "sequence": int(record.get("sequence", 0)),
                "hot": False,
            }
            continue
        last_notified_at = record.get("last_notified_at")
        last_cost = record.get("last_cost_usd")
        last_overhead = record.get("last_overhead_percent")
        first_alert = not isinstance(last_notified_at, (int, float))
        rising = (
            isinstance(last_cost, (int, float))
            and isinstance(last_overhead, (int, float))
            and cost > last_cost
            and overhead > last_overhead
        )
        may_renotify = (
            isinstance(last_notified_at, (int, float))
            and now - last_notified_at >= ALERT_RENOTIFY_SECONDS
        )
        if first_alert or (may_renotify and rising):
            record["sequence"] = int(record.get("sequence", 0)) + 1
            record["last_notified_at"] = now
            record["last_cost_usd"] = cost
            record["last_overhead_percent"] = overhead
        record["hot"] = True
        state[key] = record
        session["notification"] = {
            "sequence": int(record.get("sequence", 0)),
            "hot": True,
            "last_notified_at": record.get("last_notified_at"),
            "last_cost_usd": record.get("last_cost_usd"),
            "last_overhead_percent": record.get("last_overhead_percent"),
        }
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
) -> Iterator[tuple[str | None, str | None, str | None, list[tuple[float, int]]]]:
    """Read completed task curves and trustworthy model-effort-speed cohorts."""
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
                        configuration = single_configuration(main_events)
                        model, effort, speed = (
                            configuration
                            if configuration is not None
                            else (None, None, None)
                        )
                        yield model, effort, speed, series
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
            configuration = single_configuration(main_events)
            model, effort, speed = (
                configuration if configuration is not None else (None, None, None)
            )
            yield model, effort, speed, series


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


def configuration_key(model: str, effort: str, speed: str) -> str:
    """Build a stable model, effort, and pricing-tier baseline key."""
    return f"{model}::{effort}::{speed}"


def next_ten_forecast(costs: list[float], provider: str) -> float:
    """Forecast the next ten tasks from the last ten comparable tasks."""
    del provider
    recent = costs[-FORECAST_WINDOW:]
    return sum(recent) / len(recent) * 10 if recent else 0.0


def scaled_precompact_forecast(
    costs: list[float], current_context: int, precompact_context: int
) -> float | None:
    """Scale the pre-compact trend until enough post-compact prompts exist."""
    if current_context <= 0 or precompact_context <= 0:
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
    since = now - BASELINE_LOOKBACK_SECONDS
    for provider in ("claude", "codex"):
        provider_series: list[list[tuple[float, int]]] = []
        configuration_series: dict[str, list[list[tuple[float, int]]]] = defaultdict(
            list
        )
        configuration_labels: dict[str, tuple[str, str, str]] = {}
        for model, effort, speed, series in historical_task_series(
            provider, since, prices
        ):
            provider_series.append(series)
            if model is not None and effort is not None and speed is not None:
                key = configuration_key(model, effort, speed)
                configuration_labels[key] = (model, effort, speed)
                configuration_series[key].append(series)
        providers[provider] = cumulative_median_checkpoints(provider_series)
        configurations[provider] = {}
        for key, cohort_series in configuration_series.items():
            model, effort, speed = configuration_labels[key]
            config_checkpoints = cumulative_median_checkpoints(cohort_series)
            if config_checkpoints:
                configurations[provider][key] = {
                    "model": model,
                    "effort": effort,
                    "speed": speed,
                    "checkpoints": config_checkpoints,
                }
    compact_windows = claude_compact_next_ten_costs(since, prices)
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "lookback_days": BASELINE_LOOKBACK_SECONDS // 86400,
        "milestones": list(BASELINE_MILESTONES),
        "median_method": "monotonic_checkpoint_cohort_medians",
        "providers": providers,
        "configurations": configurations,
        "forecasts": {
            "claude_after_compact_next_10_usd": round(float(median(compact_windows)), 6)
            if compact_windows
            else 0.0,
            "claude_after_compact_samples": len(compact_windows),
        },
    }


def cumulative_median_checkpoints(
    series: list[list[tuple[float, int]]],
) -> list[dict[str, object]]:
    """Return a monotonic cumulative median over each checkpoint's reached cohort."""
    checkpoints: list[dict[str, object]] = []
    previous_cost = 0.0
    previous_tokens = 0
    for iteration in BASELINE_MILESTONES:
        cohort = [row for row in series if len(row) >= iteration]
        values = [
            (
                sum(cost for cost, _ in row[:iteration]),
                sum(tokens for _, tokens in row[:iteration]),
            )
            for row in cohort
        ]
        if len(values) < 3:
            continue
        median_cost = max(previous_cost, float(median(value[0] for value in values)))
        median_tokens = max(previous_tokens, int(median(value[1] for value in values)))
        checkpoints.append(
            {
                "iterations": iteration,
                "sessions": len(values),
                "median_cost_usd": round(median_cost, 6),
                "median_tokens": median_tokens,
            }
        )
        previous_cost = median_cost
        previous_tokens = median_tokens
    return checkpoints


def load_baselines(
    now: float, prices: dict[str, dict[str, float]]
) -> dict[str, object]:
    """Reuse a recent baseline so the minute collector only reads active sessions."""
    try:
        baseline = json.loads(baseline_path().read_text())
        generated_at = (
            parse_timestamp(baseline.get("generated_at"))
            if isinstance(baseline, dict)
            else None
        )
        forecasts = baseline.get("forecasts") if isinstance(baseline, dict) else None
        configurations = (
            baseline.get("configurations") if isinstance(baseline, dict) else None
        )
        if (
            isinstance(baseline, dict)
            and baseline.get("schema_version") == BASELINE_SCHEMA_VERSION
            and baseline.get("milestones") == list(BASELINE_MILESTONES)
            and baseline.get("median_method") == "monotonic_checkpoint_cohort_medians"
            and isinstance(forecasts, dict)
            and isinstance(configurations, dict)
            and generated_at is not None
            and now - generated_at < BASELINE_REFRESH_SECONDS
        ):
            return baseline
    except (OSError, json.JSONDecodeError):
        pass
    baseline = build_baselines(now, prices)
    destination = baseline_path()
    write_private_json(destination, baseline)
    return baseline


def baseline_comparison(
    provider: str,
    task_count: int,
    token_count: int,
    baseline: dict[str, object],
    model: str = "unknown",
    effort: str = "standard",
    speed: str = "standard",
    since_compact: bool = False,
    cost_usd: float | None = None,
    comparison_scope: Literal["auto", "provider", "model_effort_speed"] = "auto",
) -> dict[str, object] | None:
    providers = baseline.get("providers")
    provider_checkpoints = (
        providers.get(provider) if isinstance(providers, dict) else None
    )
    checkpoints = provider_checkpoints
    scope = "provider"
    configurations = baseline.get("configurations")
    provider_configurations = (
        configurations.get(provider) if isinstance(configurations, dict) else None
    )
    configuration = (
        provider_configurations.get(configuration_key(model, effort, speed))
        if isinstance(provider_configurations, dict)
        else None
    )
    config_checkpoints = (
        configuration.get("checkpoints") if isinstance(configuration, dict) else None
    )
    config_eligible = (
        [
            item
            for item in config_checkpoints
            if isinstance(item, dict)
            and isinstance(item.get("iterations"), int)
            and int(item.get("sessions", 0)) >= 3
        ]
        if isinstance(config_checkpoints, list)
        else []
    )
    if comparison_scope == "model_effort_speed":
        checkpoints = config_eligible
        scope = "model_effort_speed"
    elif comparison_scope == "auto" and config_eligible:
        checkpoints = config_eligible
        scope = "model_effort_speed"
    if not isinstance(checkpoints, list):
        return None
    eligible = [
        item
        for item in checkpoints
        if isinstance(item, dict) and isinstance(item.get("iterations"), int)
    ]
    if not eligible:
        return None
    if task_count < min(int(item["iterations"]) for item in eligible):
        if (
            comparison_scope == "auto"
            and scope == "model_effort_speed"
            and isinstance(provider_checkpoints, list)
        ):
            checkpoints = provider_checkpoints
            scope = "provider"
            eligible = [
                item
                for item in checkpoints
                if isinstance(item, dict) and isinstance(item.get("iterations"), int)
            ]
        if not eligible or task_count < min(
            int(item["iterations"]) for item in eligible
        ):
            return None
    ordered = sorted(eligible, key=lambda item: int(item["iterations"]))
    lower_index = (
        bisect_right([int(item["iterations"]) for item in ordered], task_count) - 1
    )
    lower = ordered[lower_index]
    upper = ordered[lower_index + 1] if lower_index + 1 < len(ordered) else None
    if task_count == int(lower["iterations"]):
        upper = lower
    if upper is None and len(ordered) > 1:
        lower = ordered[-2]
        upper = ordered[-1]
    if upper is None and len(ordered) == 1:
        upper = lower
        lower = {
            "iterations": 0,
            "sessions": upper.get("sessions", 0),
            "median_cost_usd": 0.0,
            "median_tokens": 0,
        }
    if upper is None:
        return None

    lower_iterations = int(lower["iterations"])
    lower_tokens = lower.get("median_tokens")
    upper_iterations = int(upper["iterations"])
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
        "speed": speed,
    }
