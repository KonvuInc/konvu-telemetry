"""Build and persist the dashboard's provider-neutral session snapshot."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .analytics import (
    apply_notification_tracking,
    baseline_comparison,
    claude_compact_times,
    context_usage_history,
    deduplicate_usage_events,
    iteration_series,
    load_baselines,
    locate_compactions,
    next_ten_forecast,
    scaled_precompact_forecast,
    single_configuration,
)
from .config import (
    ACTIVITY_FRESHNESS_SECONDS,
    DASHBOARD_SESSION_WINDOW_SECONDS,
    DEFAULT_LIVE_WINDOW_SECONDS,
    FORECAST_MIN_SAMPLES,
    FORECAST_WINDOW,
    MAX_CONTEXT_HISTORY_POINTS,
    MAX_SUMMARY_ITERATION_POINTS,
    ROLLING_WINDOW_SECONDS,
    SESSION_FILE_RETENTION_SECONDS,
)
from .fleet_telemetry import enrich_snapshot
from .live import CodexLiveFile, IncrementalLiveState
from .models import UsageEvent
from .parsers import (
    claude_client_in_file,
    claude_subagent_statuses,
    claude_titles_in_file,
    codex_client_in_file,
    codex_events_in_file,
    codex_session_id,
    codex_subagent_is_live,
    codex_subagent_parent,
    codex_task_starts,
    codex_task_tool_counts,
    codex_title_in_file,
    events_in_file,
    live_codex_transcripts,
    live_transcripts,
    spawned_agent_labels,
    spawned_agent_times,
    transcript_session_id,
    user_prompt_times_by_session,
)
from .pricing import (
    cache_read_rate,
    claude_context_window,
    cost_status,
    event_cost,
    load_pricing,
    price_for,
)
from .storage import (
    home_dir,
    parse_timestamp,
    pinned_sessions,
    session_path,
    snapshot_path,
    valid_session_id,
    write_private_json,
    write_private_json_if_changed,
)


def sampled_rows(rows: list[object], limit: int) -> list[object]:
    """Keep evenly spaced history rows, including both endpoints."""
    if limit < 2 or len(rows) <= limit:
        return rows
    indexes = {round(index * (len(rows) - 1) / (limit - 1)) for index in range(limit)}
    return [row for index, row in enumerate(rows) if index in indexes]


def sampled_rows_with_tail(
    rows: list[object], limit: int, tail_size: int
) -> list[object]:
    """Downsample old rows while retaining the exact recent comparison window."""
    if limit <= 0:
        return []
    if len(rows) <= limit:
        return rows
    if limit <= tail_size:
        return rows[-limit:]
    return sampled_rows(rows[:-tail_size], limit - tail_size) + rows[-tail_size:]


def summary_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    """Remove inspector-only detail from the frequently polled dashboard payload."""
    raw_sessions = snapshot.get("sessions")
    generated_at = parse_timestamp(snapshot.get("generated_at"))
    sessions: list[dict[str, object]] = []
    for session in raw_sessions if isinstance(raw_sessions, list) else []:
        if not isinstance(session, dict):
            continue
        last_activity = parse_timestamp(session.get("last_activity_at"))
        if (
            generated_at is not None
            and last_activity is not None
            and generated_at - last_activity > DASHBOARD_SESSION_WINDOW_SECONDS
        ):
            continue
        summary = dict(session)
        summary.pop("context_history", None)
        summary.pop("subagents", None)
        iterations = summary.get("iterations")
        if isinstance(iterations, list):
            summary["iterations"] = sampled_rows_with_tail(
                iterations, MAX_SUMMARY_ITERATION_POINTS, 6
            )
        sessions.append(summary)
    return {**snapshot, "sessions": sessions}


def build_snapshot(
    now: float, live_state: IncrementalLiveState | None = None
) -> dict[str, object]:
    """Summarise currently active Claude Code and Codex transcripts."""
    prices = load_pricing()
    baselines = load_baselines(
        now, prices, refresh_in_background=live_state is not None
    )
    grouped: dict[str, list[UsageEvent]] = defaultdict(list)
    prompt_times: dict[str, list[float]] = defaultdict(list)
    compact_times: dict[str, list[float]] = defaultdict(list)
    agent_spawn_times: dict[tuple[str, str], float] = {}
    agent_spawn_labels: dict[tuple[str, str], str] = {}
    claude_subagent_statuses_by_session: dict[str, dict[str, bool]] = defaultdict(dict)
    claude_titles: dict[str, str] = {}
    claude_clients: dict[str, str] = {}
    claude_transcripts, codex_transcripts = (
        live_state.refresh(now)
        if live_state is not None
        else (live_transcripts(now), live_codex_transcripts(now))
    )
    for transcript in claude_transcripts:
        claude_cached = (
            live_state.claude[transcript] if live_state is not None else None
        )
        root_session_id = (
            claude_cached.session_id
            if claude_cached is not None
            else transcript_session_id(transcript)
        )
        if root_session_id is not None and transcript.stem == root_session_id:
            claude_subagent_statuses_by_session[root_session_id].update(
                live_state.claude_statuses(transcript, now)
                if live_state is not None
                else claude_subagent_statuses(transcript, now)
            )
        titles = (
            claude_cached.titles
            if claude_cached is not None
            else claude_titles_in_file(transcript)
        )
        for session_id, title in titles.items():
            claude_titles.setdefault(session_id, title)
        clients = (
            claude_cached.clients
            if claude_cached is not None
            else claude_client_in_file(transcript)
        )
        for session_id, client in clients.items():
            if client == "desktop" or session_id not in claude_clients:
                claude_clients[session_id] = client
        prompts = (
            claude_cached.prompts
            if claude_cached is not None
            else user_prompt_times_by_session(transcript)
        )
        compacts = (
            claude_cached.compacts
            if claude_cached is not None
            else {
                session_id: claude_compact_times(transcript) for session_id in prompts
            }
        )
        for session_id, timestamps in prompts.items():
            prompt_times[session_id] = sorted(
                set(prompt_times[session_id] + timestamps)
            )
            compact_times[session_id] = sorted(
                set(compact_times[session_id] + compacts.get(session_id, []))
            )
        spawns = (
            claude_cached.spawns
            if claude_cached is not None
            else spawned_agent_times(transcript)
        )
        for key, timestamp in spawns.items():
            agent_spawn_times.setdefault(key, timestamp)
        for key, label in spawned_agent_labels(transcript).items():
            agent_spawn_labels.setdefault(key, label)
        source_events = (
            claude_cached.events.values()
            if claude_cached is not None
            else events_in_file(transcript)
        )
        for event in source_events:
            grouped[event.session_id].append(event)
    sessions: list[dict[str, object]] = []
    for session_id, events in grouped.items():
        events = deduplicate_usage_events(events)
        claude_costs = {id(event): event_cost(event, prices) for event in events}
        known_costs = [cost for cost in claude_costs.values() if cost is not None]
        last_event = events[-1]
        subagent_events: dict[str, list[UsageEvent]] = defaultdict(list)
        for event in events:
            if event.is_subagent and event.agent_id:
                subagent_events[event.agent_id].append(event)
        spawned_agent_ids = {
            agent_id
            for parent_session_id, agent_id in agent_spawn_times
            if parent_session_id == session_id
        }
        all_subagent_ids = spawned_agent_ids | set(subagent_events)
        statuses = claude_subagent_statuses_by_session.get(session_id, {})
        entry_context_tokens = sum(
            min(agent_events, key=lambda event: event.timestamp).usage.context_tokens
            for agent_events in subagent_events.values()
        )
        subagent_costs = [
            cost
            for event in events
            for cost in [claude_costs[id(event)]]
            if event.is_subagent and cost is not None
        ]
        costs_by_prompt: dict[float, float] = defaultdict(float)
        unattributed_cost = 0.0
        for event in events:
            cost = claude_costs[id(event)]
            if cost is None:
                continue
            attribution_time = event.timestamp
            if event.is_subagent:
                if event.agent_id is None:
                    attribution_time = event.timestamp
                else:
                    attribution_time = agent_spawn_times.get(
                        (session_id, event.agent_id), event.timestamp
                    )
            starts = prompt_times.get(session_id, [])
            prompt_index = bisect_right(starts, attribution_time) - 1
            if prompt_index >= 0:
                costs_by_prompt[starts[prompt_index]] += cost
            else:
                unattributed_cost += cost
        starts = prompt_times.get(session_id, [])
        context_attribution = {
            id(event): agent_spawn_times.get(
                (session_id, event.agent_id), event.timestamp
            )
            for event in events
            if event.is_subagent and event.agent_id is not None
        }
        if starts and unattributed_cost:
            costs_by_prompt[starts[0]] += unattributed_cost
        # Keep prompt costs aligned with their boundaries: a prompt with no model event is still a prompt.
        completed_prompt_costs = [costs_by_prompt.get(start, 0.0) for start in starts]
        forecast_prompt_costs = completed_prompt_costs
        last_task_cost = completed_prompt_costs[-1] if completed_prompt_costs else 0.0
        next_10_forecast: float | None = (
            next_ten_forecast(forecast_prompt_costs, "claude")
            if len(forecast_prompt_costs) >= FORECAST_MIN_SAMPLES
            else None
        )
        last_prompt_start = starts[-1] if starts else None
        latest_compact = (
            compact_times.get(session_id, [])[-1]
            if compact_times.get(session_id)
            else None
        )
        comparison_task_count = len(starts)
        comparison_tokens = total_tokens = sum(
            event.usage.total_tokens for event in events
        )
        total_cost_status, unpriced_event_count = cost_status(events, prices)
        if total_cost_status != "complete":
            next_10_forecast = None
        comparison_cost = sum(known_costs) if total_cost_status == "complete" else None
        since_compact = False
        forecast_mode = "rolling"
        if latest_compact is not None:
            first_post_compact = bisect_right(starts, latest_compact)
            post_compact_costs = forecast_prompt_costs[first_post_compact:]
            post_compact_events = [
                event for event in events if event.timestamp > latest_compact
            ]
            comparison_task_count = len(post_compact_costs)
            comparison_tokens = sum(
                event.usage.total_tokens for event in post_compact_events
            )
            comparison_cost = (
                sum(
                    cost
                    for event in post_compact_events
                    for cost in [claude_costs[id(event)]]
                    if cost is not None
                )
                if total_cost_status == "complete"
                else None
            )
            since_compact = True
            if len(post_compact_costs) >= FORECAST_MIN_SAMPLES:
                next_10_forecast = next_ten_forecast(post_compact_costs, "claude")
                forecast_mode = "post_compact"
            else:
                main_thread_events = [
                    event for event in events if not event.is_subagent
                ]
                precompact_events = [
                    event
                    for event in main_thread_events
                    if event.timestamp <= latest_compact
                ]
                current_context = (
                    main_thread_events[-1].usage.context_tokens
                    if main_thread_events
                    else 0
                )
                precompact_context = (
                    precompact_events[-1].usage.context_tokens
                    if precompact_events
                    else 0
                )
                next_10_forecast = scaled_precompact_forecast(
                    forecast_prompt_costs[:first_post_compact],
                    current_context,
                    precompact_context,
                )
                forecast_mode = (
                    "post_compact_scaled"
                    if next_10_forecast is not None
                    else "warming_up"
                )
        last_task_tool_calls = sum(
            event.tool_calls
            for event in events
            if not event.is_subagent
            and last_prompt_start is not None
            and event.timestamp >= last_prompt_start
        )
        task_count = len(starts)
        main_thread_events = [event for event in events if not event.is_subagent]
        context_event = main_thread_events[-1] if main_thread_events else last_event
        baseline_events = [
            event
            for event in main_thread_events
            if latest_compact is None or event.timestamp > latest_compact
        ]
        baseline_configuration = single_configuration(baseline_events)
        baseline_model, baseline_effort, baseline_speed = (
            baseline_configuration
            if baseline_configuration is not None
            else ("unknown", "standard", "standard")
        )
        claude_provider_baseline = baseline_comparison(
            "claude",
            comparison_task_count,
            comparison_tokens,
            baselines,
            model=baseline_model,
            effort=baseline_effort,
            speed=baseline_speed,
            since_compact=since_compact,
            cost_usd=comparison_cost,
            comparison_scope="provider",
        )
        claude_configuration_baseline = baseline_comparison(
            "claude",
            comparison_task_count,
            comparison_tokens,
            baselines,
            model=baseline_model,
            effort=baseline_effort,
            speed=baseline_speed,
            since_compact=since_compact,
            cost_usd=comparison_cost,
            comparison_scope="model_effort_speed",
        )
        claude_baseline = claude_configuration_baseline
        if since_compact and comparison_task_count < FORECAST_WINDOW:
            claude_baseline = None
            claude_provider_baseline = None
            claude_configuration_baseline = None
        if total_cost_status != "complete":
            next_10_forecast = None
            claude_baseline = None
            claude_provider_baseline = None
            claude_configuration_baseline = None
        sessions.append(
            {
                "id": session_id,
                "provider": "claude",
                "client": claude_clients.get(session_id, "unknown"),
                "title": claude_titles.get(
                    session_id, f"Claude session {session_id[:8]}"
                ),
                "model": context_event.model,
                "effort": context_event.effort,
                "speed": context_event.usage.speed,
                "comparison_configuration": (
                    {
                        "model": baseline_model,
                        "effort": baseline_effort,
                        "speed": baseline_speed,
                    }
                    if baseline_configuration is not None
                    else None
                ),
                "last_activity_at": datetime.fromtimestamp(
                    last_event.timestamp, timezone.utc
                ).isoformat(),
                "rolling_window_seconds": ROLLING_WINDOW_SECONDS,
                "total_cost_usd": round(sum(known_costs), 6),
                "cost_status": total_cost_status,
                "unpriced_event_count": unpriced_event_count,
                "last_task_cost_usd": round(last_task_cost, 6),
                "projected_next_10_tasks_usd": round(next_10_forecast, 6)
                if isinstance(next_10_forecast, (int, float))
                else None,
                "forecast_mode": forecast_mode,
                "iterations": iteration_series(
                    starts,
                    completed_prompt_costs,
                    events,
                    total_cost_status == "complete",
                ),
                "context_history": context_usage_history(
                    starts, events, prices, context_attribution
                ),
                "task_count": task_count,
                "since_compact": since_compact,
                "last_task_tool_calls": last_task_tool_calls,
                "baseline": claude_baseline,
                "baselines": {
                    "provider": claude_provider_baseline,
                    "model_effort_speed": claude_configuration_baseline,
                },
                "context_tokens": context_event.usage.context_tokens,
                "context_window_tokens": claude_context_window(
                    context_event.model, context_event.usage.context_tokens, prices
                ),
                "context_window_source": "model_pricing"
                if price_for(context_event.model, prices)
                else "estimated",
                "cache_read_usd_per_mtok": cache_read_rate(context_event.model, prices),
                "active_subagents": sum(
                    1 for agent_id in all_subagent_ids if statuses.get(agent_id, False)
                ),
                "subagent_total": len(all_subagent_ids),
                "subagent_entry_context_tokens": entry_context_tokens,
                "subagent_cost_usd": round(sum(subagent_costs), 6),
                "subagents": [
                    {
                        "id": agent_id,
                        "label": agent_spawn_labels.get(
                            (session_id, agent_id), "Claude subagent"
                        ),
                        "entry_context_tokens": (
                            min(
                                subagent_events[agent_id],
                                key=lambda event: event.timestamp,
                            ).usage.context_tokens
                            if agent_id in subagent_events
                            else 0
                        ),
                        "live": statuses.get(agent_id, False),
                        "cost_usd": round(
                            sum(
                                cost
                                for event in subagent_events.get(agent_id, [])
                                for cost in [claude_costs[id(event)]]
                                if cost is not None
                            ),
                            6,
                        )
                        if any(
                            claude_costs[id(event)] is not None
                            for event in subagent_events.get(agent_id, [])
                        )
                        else None,
                    }
                    for agent_id in sorted(all_subagent_ids)
                ],
                "priced_events": len(known_costs),
                "token_usage": {
                    "input": sum(event.usage.input_tokens for event in events),
                    "output": sum(event.usage.output_tokens for event in events),
                    "cache_write": sum(
                        event.usage.cache_write_tokens for event in events
                    ),
                    "cache_read": sum(
                        event.usage.cache_read_tokens for event in events
                    ),
                },
            }
        )
    codex_grouped: dict[str, list[UsageEvent]] = defaultdict(list)
    codex_task_boundaries: dict[str, list[float]] = defaultdict(list)
    codex_task_tools: dict[str, dict[float, int]] = defaultdict(dict)
    codex_titles: dict[str, str] = {}
    codex_clients: dict[str, str] = {}
    codex_subagent_events: dict[str, list[UsageEvent]] = defaultdict(list)
    codex_subagent_entries: dict[
        str, list[tuple[str, str, int, bool, list[UsageEvent]]]
    ] = defaultdict(list)
    codex_sources: list[
        tuple[Path, CodexLiveFile | None, str, tuple[str, str] | None]
    ] = []
    for transcript in codex_transcripts:
        codex_cached = live_state.codex[transcript] if live_state is not None else None
        session_id = (
            codex_cached.session_id
            if codex_cached is not None
            else codex_session_id(transcript)
        )
        if not valid_session_id(session_id):
            continue
        subagent = (
            codex_cached.parent
            if codex_cached is not None
            else codex_subagent_parent(transcript)
        )
        codex_sources.append((transcript, codex_cached, session_id, subagent))

    parents = {
        session_id: parent
        for _, _, session_id, parent in codex_sources
        if parent is not None
    }

    def codex_root(session_id: str) -> str:
        current = session_id
        visited: set[str] = set()
        while current not in visited and current in parents:
            visited.add(current)
            current = parents[current][0]
        return current

    for transcript, codex_cached, session_id, subagent in codex_sources:
        if subagent is not None:
            _, label = subagent
            parent_id = codex_root(session_id)
            raw_child_events = (
                list(codex_cached.events)
                if codex_cached is not None
                else list(codex_events_in_file(transcript))
            )
            child_events = [
                replace(event, is_subagent=True, agent_id=session_id)
                for event in raw_child_events
            ]
            codex_subagent_events[parent_id].extend(child_events)
            entry_context = child_events[0].usage.context_tokens if child_events else 0
            is_live = (
                codex_cached.latest_started_at > codex_cached.latest_terminal_at
                and now - codex_cached.latest_started_at <= ACTIVITY_FRESHNESS_SECONDS
                if codex_cached is not None
                else codex_subagent_is_live(transcript, now)
            )
            codex_subagent_entries[parent_id].append(
                (session_id, label, entry_context, is_live, child_events)
            )
            continue
        codex_title = (
            codex_cached.title
            if codex_cached is not None and codex_cached.title
            else codex_title_in_file(transcript)
        )
        if codex_title:
            codex_titles.setdefault(session_id, codex_title)
        client = (
            codex_cached.client
            if codex_cached is not None
            else codex_client_in_file(transcript)
        )
        if client != "unknown" or session_id not in codex_clients:
            codex_clients[session_id] = client
        codex_grouped[session_id].extend(
            codex_cached.events
            if codex_cached is not None
            else codex_events_in_file(transcript)
        )
        starts = (
            codex_cached.task_starts
            if codex_cached is not None
            else codex_task_starts(transcript)
        )
        codex_task_boundaries[session_id] = sorted(
            set(codex_task_boundaries[session_id] + starts)
        )
        tools = (
            codex_cached.tool_counts
            if codex_cached is not None
            else codex_task_tool_counts(transcript)
        )
        for start, count in tools.items():
            codex_task_tools[session_id][start] = (
                codex_task_tools[session_id].get(start, 0) + count
            )
    for session_id, events in codex_grouped.items():
        if not events:
            continue
        events.sort(key=lambda event: event.timestamp)
        codex_costs = [event_cost(event, prices) for event in events]
        known_costs = [cost for cost in codex_costs if cost is not None]
        child_events = codex_subagent_events.get(session_id, [])
        child_costs = [event_cost(event, prices) for event in child_events]
        known_child_costs = [cost for cost in child_costs if cost is not None]
        child_entries = codex_subagent_entries.get(session_id, [])
        costs_by_task: dict[float, float] = defaultdict(float)
        starts = codex_task_boundaries.get(session_id, [])
        for event, cost in zip(events, codex_costs):
            if cost is None:
                continue
            task_index = bisect_right(starts, event.timestamp) - 1
            if task_index >= 0:
                costs_by_task[starts[task_index]] += cost
        for event, cost in zip(child_events, child_costs):
            if cost is None:
                continue
            task_index = bisect_right(starts, event.timestamp) - 1
            if task_index >= 0:
                costs_by_task[starts[task_index]] += cost
        task_costs = [costs_by_task.get(start, 0.0) for start in starts]
        last_task_cost = task_costs[-1] if task_costs else 0.0
        next_10_forecast = (
            next_ten_forecast(task_costs, "codex")
            if len(task_costs) >= FORECAST_MIN_SAMPLES
            else None
        )
        task_count = len(starts)
        all_events = [*events, *child_events]
        last_event = max(all_events, key=lambda event: event.timestamp)
        total_tokens = sum(event.usage.total_tokens for event in all_events)
        total_cost_status, unpriced_event_count = cost_status(all_events, prices)
        if total_cost_status != "complete":
            next_10_forecast = None
        last_task_start = starts[-1] if starts else None
        baseline_configuration = single_configuration(events)
        baseline_model, baseline_effort, baseline_speed = (
            baseline_configuration
            if baseline_configuration is not None
            else ("unknown", "standard", "standard")
        )
        codex_provider_baseline = baseline_comparison(
            "codex",
            task_count,
            total_tokens,
            baselines,
            model=baseline_model,
            effort=baseline_effort,
            speed=baseline_speed,
            cost_usd=sum(known_costs) + sum(known_child_costs),
            comparison_scope="provider",
        )
        codex_configuration_baseline = baseline_comparison(
            "codex",
            task_count,
            total_tokens,
            baselines,
            model=baseline_model,
            effort=baseline_effort,
            speed=baseline_speed,
            cost_usd=sum(known_costs) + sum(known_child_costs),
            comparison_scope="model_effort_speed",
        )
        codex_baseline = codex_configuration_baseline
        if total_cost_status != "complete":
            codex_baseline = None
            codex_provider_baseline = None
            codex_configuration_baseline = None
        sessions.append(
            {
                "id": session_id,
                "provider": "codex",
                "client": codex_clients.get(session_id, "unknown"),
                "title": codex_titles.get(
                    session_id, f"Codex session {session_id[:8]}"
                ),
                "model": last_event.model,
                "effort": last_event.effort,
                "speed": last_event.usage.speed,
                "comparison_configuration": (
                    {
                        "model": baseline_model,
                        "effort": baseline_effort,
                        "speed": baseline_speed,
                    }
                    if baseline_configuration is not None
                    else None
                ),
                "last_activity_at": datetime.fromtimestamp(
                    last_event.timestamp, timezone.utc
                ).isoformat(),
                "total_cost_usd": round(sum(known_costs) + sum(known_child_costs), 6),
                "cost_status": total_cost_status,
                "unpriced_event_count": unpriced_event_count,
                "last_task_cost_usd": round(last_task_cost, 6),
                "projected_next_10_tasks_usd": round(next_10_forecast, 6)
                if isinstance(next_10_forecast, (int, float))
                else None,
                "iterations": iteration_series(
                    starts, task_costs, all_events, total_cost_status == "complete"
                ),
                "context_history": context_usage_history(starts, all_events, prices),
                "task_count": task_count,
                "last_task_tool_calls": codex_task_tools[session_id].get(
                    last_task_start, 0
                )
                if last_task_start is not None
                else 0,
                "baseline": codex_baseline,
                "baselines": {
                    "provider": codex_provider_baseline,
                    "model_effort_speed": codex_configuration_baseline,
                },
                "active_subagents": sum(
                    1 for _, _, _, is_live, _ in child_entries if is_live
                ),
                "subagent_total": len(child_entries),
                "subagent_entry_context_tokens": sum(
                    context for _, _, context, _, _ in child_entries
                ),
                "subagent_cost_usd": round(sum(known_child_costs), 6),
                "subagents": [
                    {
                        "id": agent_id,
                        "label": label,
                        "entry_context_tokens": context,
                        "live": is_live,
                        "cost_usd": round(
                            sum(
                                cost
                                for event in agent_events
                                for cost in [event_cost(event, prices)]
                                if cost is not None
                            ),
                            6,
                        )
                        if any(
                            event_cost(event, prices) is not None
                            for event in agent_events
                        )
                        else None,
                    }
                    for agent_id, label, context, is_live, agent_events in child_entries
                ],
                "priced_events": len(
                    [cost for cost in [*codex_costs, *child_costs] if cost is not None]
                ),
                "context_tokens": last_event.usage.context_tokens,
                "cache_read_usd_per_mtok": cache_read_rate(last_event.model, prices),
                "context_window_tokens": last_event.context_window_tokens,
                "token_usage": {
                    "input": sum(event.usage.input_tokens for event in all_events),
                    "output": sum(event.usage.output_tokens for event in all_events),
                    "cache_write": sum(
                        event.usage.cache_write_tokens for event in all_events
                    ),
                    "cache_read": sum(
                        event.usage.cache_read_tokens for event in all_events
                    ),
                },
            }
        )
    live_ids = {session["id"] for session in sessions}
    sessions.extend(row for row in pinned_sessions() if row.get("id") not in live_ids)
    snapshot = {
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "liveness_window_seconds": DEFAULT_LIVE_WINDOW_SECONDS,
        "baselines": baselines,
        "sessions": sessions,
    }
    enrich_snapshot(snapshot, claude_transcripts, codex_transcripts, now)
    locate_compactions(snapshot)
    for session in sessions:
        history = session.get("context_history")
        if isinstance(history, list):
            session["context_history"] = sampled_rows(
                history, MAX_CONTEXT_HISTORY_POINTS
            )
    account_quotas = snapshot.get("account_quotas")
    apply_notification_tracking(
        sessions,
        now,
        account_quotas if isinstance(account_quotas, dict) else None,
    )
    sessions.sort(
        key=lambda session: str(session.get("last_activity_at") or ""), reverse=True
    )
    return snapshot


def write_snapshot(snapshot: dict[str, object]) -> None:
    destination = snapshot_path()
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list):
        write_private_json(destination, summary_snapshot(snapshot))
        return
    for session in sessions:
        if not isinstance(session, dict):
            continue
        provider = session.get("provider")
        session_id = session.get("id")
        if not isinstance(provider, str) or not isinstance(session_id, str):
            continue
        try:
            session_destination = session_path(provider, session_id)
        except ValueError:
            continue
        write_private_json_if_changed(session_destination, session)
    write_private_json(destination, summary_snapshot(snapshot))
    sessions_directory = home_dir() / "sessions"
    cutoff = time.time() - SESSION_FILE_RETENTION_SECONDS
    try:
        for candidate in sessions_directory.glob("*.json"):
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink(missing_ok=True)
    except OSError:
        pass


def load_snapshot() -> dict[str, object] | None:
    try:
        snapshot = json.loads(snapshot_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return snapshot if isinstance(snapshot, dict) else None
