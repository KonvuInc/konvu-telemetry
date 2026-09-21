"""Read transcript metadata for dashboard activity, compactions, and account quotas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Literal

from .config import FILE_CACHE_LIMIT


Provider = Literal["claude", "codex"]
ACTIVITY_FRESHNESS_SECONDS = 300
CODEX_TOOL_ITEMS = {
    "CommandExecution",
    "McpToolCall",
    "WebSearch",
    "ViewImageToolCall",
    "ImageGeneration",
}


@dataclass
class TranscriptTelemetry:
    session_id: str | None = None
    is_subagent: bool = False
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    activity: list[tuple[float, str, str | None]] = field(default_factory=list)
    configurations: list[tuple[float, str | None, str | None]] = field(
        default_factory=list
    )
    completions: list[tuple[float, str | None]] = field(default_factory=list)
    compactions: list[dict[str, object]] = field(default_factory=list)
    quotas: dict[str, tuple[float, list[dict[str, object]]]] = field(
        default_factory=dict
    )
    task_tools: dict[float, int] = field(default_factory=dict)


_CACHE: dict[tuple[str, str], tuple[int, int, TranscriptTelemetry]] = {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def is_claude_prompt(record: dict[str, object]) -> bool:
    """Identify user prompts without counting tool results or synthetic summaries."""
    if (
        record.get("isSidechain") is True
        or record.get("isMeta") is True
        or record.get("isCompactSummary") is True
    ):
        return False
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        command_prefixes = (
            "<command-name>",
            "<local-command-stdout>",
            "<system-reminder>",
        )
        return bool(content.strip()) and not content.lstrip().startswith(
            command_prefixes
        )
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") in ("text", "image", "document")
        for item in content
    )


def _quota_windows(raw: dict[str, object], observed: float) -> list[dict[str, object]]:
    windows: list[dict[str, object]] = []
    limit_id = raw.get("limit_id")
    for name in ("primary", "secondary"):
        window = raw.get(name)
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percent"))
        minutes = _number(window.get("window_minutes"))
        reset = _number(window.get("resets_at"))
        if used is None or minutes is None or minutes <= 0:
            continue
        windows.append(
            {
                "limit_id": limit_id if isinstance(limit_id, str) else "default",
                "window_minutes": minutes,
                "used_percent": min(100.0, used),
                "remaining_percent": max(0.0, 100.0 - used),
                "resets_at": _iso(reset),
                "observed_at": _iso(observed),
            }
        )
    return windows


def _read_telemetry(path: Path, provider: Provider) -> TranscriptTelemetry:
    result = TranscriptTelemetry()
    result.is_subagent = provider == "claude" and (
        path.parent.name == "subagents" or path.name.startswith("agent-")
    )
    latest_task: float | None = None
    last_compacted: float | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                timestamp = _timestamp(record.get("timestamp"))
                if timestamp is not None:
                    result.first_timestamp = (
                        min(timestamp, result.first_timestamp)
                        if result.first_timestamp is not None
                        else timestamp
                    )
                    result.last_timestamp = (
                        max(timestamp, result.last_timestamp)
                        if result.last_timestamp is not None
                        else timestamp
                    )
                payload = record.get("payload")
                payload = payload if isinstance(payload, dict) else {}
                record_type = record.get("type")
                if provider == "claude":
                    session_id = record.get("sessionId")
                    if isinstance(session_id, str):
                        result.session_id = session_id
                    if record.get("isSidechain") is True:
                        continue
                    if timestamp is None:
                        continue
                    if is_claude_prompt(record):
                        result.activity.append((timestamp, "running", None))
                    message = record.get("message")
                    if (
                        record_type == "system"
                        and record.get("subtype") == "turn_duration"
                    ):
                        result.activity.append((timestamp, "idle", None))
                        result.completions.append((timestamp, None))
                    elif (
                        isinstance(message, dict)
                        and message.get("role") == "assistant"
                        and message.get("stop_reason") == "end_turn"
                    ):
                        result.activity.append((timestamp, "idle", None))
                        result.completions.append((timestamp, None))
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        model = message.get("model")
                        effort = record.get("effort")
                        if (
                            isinstance(model, str)
                            and model.strip()
                            and model not in {"unknown", "<synthetic>"}
                        ):
                            result.configurations.append(
                                (
                                    timestamp,
                                    model,
                                    effort
                                    if isinstance(effort, str)
                                    and effort.strip()
                                    and effort != "unknown"
                                    else None,
                                )
                            )
                    if (
                        record_type == "system"
                        and record.get("subtype") == "compact_boundary"
                    ):
                        compact: dict[str, object] = {
                            "timestamp": _iso(timestamp),
                            "source": "claude_compact_boundary",
                        }
                        metadata = record.get("compactMetadata")
                        if isinstance(metadata, dict):
                            for original, target in (
                                ("preTokens", "pre_tokens"),
                                ("postTokens", "post_tokens"),
                                ("durationMs", "duration_ms"),
                            ):
                                value = _number(metadata.get(original))
                                if value is not None:
                                    compact[target] = int(value)
                        result.compactions.append(compact)
                    continue
                if record_type == "session_meta":
                    session_id = payload.get("id")
                    if isinstance(session_id, str):
                        result.session_id = session_id
                    source = payload.get("source")
                    result.is_subagent = isinstance(source, dict) and isinstance(
                        source.get("subagent"), dict
                    )
                if timestamp is None:
                    continue
                event_type = payload.get("type") if record_type == "event_msg" else None
                if record_type == "turn_context":
                    model = payload.get("model")
                    effort = payload.get("effort")
                    result.configurations.append(
                        (
                            timestamp,
                            model
                            if isinstance(model, str)
                            and model.strip()
                            and model != "unknown"
                            else None,
                            effort
                            if isinstance(effort, str)
                            and effort.strip()
                            and effort != "unknown"
                            else None,
                        )
                    )
                if event_type in ("task_started", "task_complete", "turn_aborted"):
                    turn_id = payload.get("turn_id")
                    result.activity.append(
                        (
                            timestamp,
                            "running" if event_type == "task_started" else "idle",
                            turn_id if isinstance(turn_id, str) else None,
                        )
                    )
                    if event_type == "task_started":
                        latest_task = timestamp
                        result.task_tools.setdefault(timestamp, 0)
                    elif event_type == "task_complete":
                        result.completions.append(
                            (timestamp, turn_id if isinstance(turn_id, str) else None)
                        )
                if event_type == "item_completed" and latest_task is not None:
                    item = payload.get("item")
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("type"), str)
                        and item["type"] in CODEX_TOOL_ITEMS
                    ):
                        result.task_tools[latest_task] += 1
                if record_type == "compacted":
                    result.compactions.append(
                        {"timestamp": _iso(timestamp), "source": "codex_compacted"}
                    )
                    last_compacted = timestamp
                elif event_type == "context_compacted" and (
                    last_compacted is None or not 0 <= timestamp - last_compacted <= 1
                ):
                    result.compactions.append(
                        {
                            "timestamp": _iso(timestamp),
                            "source": "codex_context_compacted",
                        }
                    )
                if event_type == "token_count":
                    quotas = payload.get("rate_limits")
                    if isinstance(quotas, dict):
                        limit_id = quotas.get("limit_id")
                        key = limit_id if isinstance(limit_id, str) else "default"
                        previous = result.quotas.get(key)
                        if previous is None or timestamp >= previous[0]:
                            result.quotas[key] = (
                                timestamp,
                                _quota_windows(quotas, timestamp),
                            )
    except (OSError, UnicodeError):
        pass
    return result


def parse_telemetry(path: Path, provider: Provider) -> TranscriptTelemetry:
    """Cache sanitized metadata until a transcript's size or mtime changes."""
    try:
        stat = path.stat()
    except OSError:
        return TranscriptTelemetry()
    key = provider, str(path)
    cached = _CACHE.get(key)
    if cached is not None and cached[:2] == (stat.st_size, stat.st_mtime_ns):
        return cached[2]
    parsed = _read_telemetry(path, provider)
    _CACHE[key] = stat.st_size, stat.st_mtime_ns, parsed
    if len(_CACHE) > FILE_CACHE_LIMIT:
        _CACHE.pop(next(iter(_CACHE)), None)
    return parsed


def _activity(
    rows: list[TranscriptTelemetry], provider: Provider, now: float
) -> dict[str, object]:
    events = sorted(
        (event for row in rows for event in row.activity), key=lambda event: event[0]
    )
    state = "unknown"
    observed: float | None = None
    current_turn: str | None = None
    for timestamp, next_state, turn_id in events:
        if (
            next_state == "idle"
            and turn_id is not None
            and current_turn is not None
            and turn_id != current_turn
        ):
            continue
        state = next_state
        observed = timestamp
        if next_state == "running":
            current_turn = turn_id
    last_timestamps = [
        row.last_timestamp for row in rows if row.last_timestamp is not None
    ]
    last_observed = max(last_timestamps) if last_timestamps else observed
    stale = last_observed is None or now - last_observed > ACTIVITY_FRESHNESS_SECONDS
    return {
        "state": "unknown" if state == "running" and stale else state,
        "last_known_state": state,
        "observed_at": _iso(observed),
        "last_record_at": _iso(last_observed),
        "stale": stale,
        "source": ("codex_task_events" if provider == "codex" else "claude_turn_events")
        if events
        else "unavailable",
    }


def _comparable_forecast(
    session: dict[str, object], rows: list[TranscriptTelemetry], replace_forecast: bool
) -> None:
    """Forecast only completed, priced prompts with the latest recorded configuration."""
    configurations = sorted(
        (configuration for row in rows for configuration in row.configurations),
        key=lambda item: item[0],
    )
    completions = [completion for row in rows for completion in row.completions]
    task_starts = {
        timestamp: turn_id
        for row in rows
        for timestamp, state, turn_id in row.activity
        if state == "running"
    }
    raw_iterations = session.get("iterations")
    iterations = (
        [row for row in raw_iterations if isinstance(row, dict)]
        if isinstance(raw_iterations, list)
        else []
    )
    for index, iteration in enumerate(iterations):
        start = _timestamp(iteration.get("started_at"))
        end = (
            _timestamp(iterations[index + 1].get("started_at"))
            if index + 1 < len(iterations)
            else None
        )
        configuration = {
            (model, effort)
            for timestamp, model, effort in configurations
            if start is not None
            and timestamp >= start
            and (end is None or timestamp < end)
        }
        model, effort = (
            next(iter(configuration)) if len(configuration) == 1 else (None, None)
        )
        turn_id = task_starts.get(start) if start is not None else None
        completed = any(
            start is not None
            and timestamp >= start
            and (end is None or timestamp < end)
            and (turn_id is None or completed_turn == turn_id)
            for timestamp, completed_turn in completions
        )
        iteration.update(
            {
                "model": model,
                "reasoning_effort": effort,
                "completed": completed,
                "priced": iteration.get("priced") is True,
            }
        )
    latest = configurations[-1] if configurations else None
    latest_model = latest[1] if latest else None
    latest_effort = latest[2] if latest else None
    if latest_effort is not None:
        session["reasoning_effort"] = latest_effort
    comparable = [
        iteration
        for iteration in iterations
        if latest_model is not None
        and latest_effort is not None
        and iteration.get("model") == latest_model
        and iteration.get("reasoning_effort") == latest_effort
        and iteration.get("completed") is True
        and iteration.get("priced") is True
        and _number(iteration.get("cost_usd")) is not None
    ][-10:]
    session["forecast_basis"] = {
        "method": "same_config_completed_prompts"
        if replace_forecast
        else "rolling_last_10_prompts",
        "sample_count": len(comparable),
        "model": latest_model,
        "effort": latest_effort,
        "coverage": "fully_priced" if comparable else "unavailable",
        "reason": None
        if comparable
        else "unknown_configuration"
        if latest_model is None or latest_effort is None
        else "no_comparable_completed_prompts",
    }
    if replace_forecast:
        session["projected_next_10_tasks_usd"] = (
            round(
                sum(float(row["cost_usd"]) for row in comparable)
                / len(comparable)
                * 10,
                6,
            )
            if comparable
            else None
        )


def enrich_snapshot(
    snapshot: dict[str, object],
    claude_paths: list[Path],
    codex_paths: list[Path],
    now: float,
) -> None:
    """Add explicit transcript telemetry without changing cost accounting."""
    grouped: dict[tuple[str, str], list[TranscriptTelemetry]] = {}
    quotas: dict[str, tuple[float, list[dict[str, object]]]] = {}
    sources: tuple[tuple[Provider, list[Path]], ...] = (
        ("claude", claude_paths),
        ("codex", codex_paths),
    )
    for provider, paths in sources:
        for path in paths:
            row = parse_telemetry(path, provider)
            if row.session_id is not None and not row.is_subagent:
                grouped.setdefault((provider, row.session_id), []).append(row)
            for limit_id, observation in row.quotas.items():
                if limit_id not in quotas or observation[0] > quotas[limit_id][0]:
                    quotas[limit_id] = observation
    sessions = snapshot.get("sessions")
    for session in sessions if isinstance(sessions, list) else []:
        if not isinstance(session, dict):
            continue
        raw_provider = session.get("provider")
        session_id = session.get("id")
        if raw_provider == "claude":
            session_provider: Provider = "claude"
        elif raw_provider == "codex":
            session_provider = "codex"
        else:
            continue
        if not isinstance(session_id, str):
            continue
        rows = grouped.get((session_provider, session_id), [])
        if not rows:
            continue
        starts = [
            row.first_timestamp for row in rows if row.first_timestamp is not None
        ]
        session["session_started_at"] = _iso(min(starts)) if starts else None
        session["activity"] = _activity(rows, session_provider, now)
        compactions = {
            str(event["timestamp"]): event for row in rows for event in row.compactions
        }
        session["compact_events"] = sorted(
            compactions.values(), key=lambda event: str(event["timestamp"])
        )
        _comparable_forecast(
            session,
            rows,
            not isinstance(session.get("projected_next_10_tasks_usd"), (int, float))
            and session.get("forecast_mode") != "warming_up",
        )
        if session_provider == "codex":
            task_tools = {
                start: count for row in rows for start, count in row.task_tools.items()
            }
            iterations = session.get("iterations")
            for iteration in iterations if isinstance(iterations, list) else []:
                if isinstance(iteration, dict):
                    start = _timestamp(iteration.get("started_at"))
                    if start in task_tools:
                        iteration["tool_calls"] = task_tools[start]
    named = {key: value for key, value in quotas.items() if key != "default"}
    if (
        named
        and "default" in quotas
        and max(value[0] for value in named.values()) >= quotas["default"][0]
    ):
        quotas.pop("default")
    observed = max((row[0] for row in quotas.values()), default=None)
    snapshot["account_quotas"] = {
        "codex": {
            "observed_at": _iso(observed),
            "source": "local_transcript",
            "windows": [
                window for limit_id in sorted(quotas) for window in quotas[limit_id][1]
            ],
        },
    }
