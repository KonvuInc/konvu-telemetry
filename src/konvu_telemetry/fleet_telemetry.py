"""Read transcript metadata for dashboard activity, compactions, and account quotas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Literal

from .config import (
    FILE_CACHE_LIMIT,
    FORECAST_MIN_SAMPLES,
    FORECAST_WINDOW,
    MAX_PARSED_RECORD_BYTES,
)
from .live import IncrementalLiveState


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
    task_tools: dict[float, int] = field(default_factory=dict)
    latest_task: float | None = None
    last_compacted: float | None = None


@dataclass
class TelemetryCacheEntry:
    identity: tuple[int, int]
    offset: int
    size: int
    mtime_ns: int
    telemetry: TranscriptTelemetry


_CACHE: dict[tuple[str, str], TelemetryCacheEntry] = {}


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


def _read_telemetry(
    path: Path,
    provider: Provider,
    result: TranscriptTelemetry | None = None,
    offset: int = 0,
) -> tuple[TranscriptTelemetry, int]:
    result = result or TranscriptTelemetry()
    next_offset = offset
    if offset == 0:
        result.is_subagent = provider == "claude" and (
            path.parent.name == "subagents" or path.name.startswith("agent-")
        )
    try:
        with path.open("rb") as transcript:
            transcript.seek(offset)
            while True:
                start = transcript.tell()
                raw_line = transcript.readline(MAX_PARSED_RECORD_BYTES + 1)
                if not raw_line:
                    break
                oversized = False
                if not raw_line.endswith(b"\n"):
                    if len(raw_line) <= MAX_PARSED_RECORD_BYTES:
                        transcript.seek(start)
                        break
                    oversized = True
                    prefix = raw_line[:MAX_PARSED_RECORD_BYTES]
                    suffix = b""
                    while True:
                        remainder = transcript.readline(MAX_PARSED_RECORD_BYTES + 1)
                        if not remainder:
                            transcript.seek(start)
                            return result, start
                        suffix = (suffix + remainder)[-MAX_PARSED_RECORD_BYTES:]
                        if remainder.endswith(b"\n"):
                            break
                    raw_line = prefix + suffix
                next_offset = transcript.tell()
                if oversized:
                    record = (
                        IncrementalLiveState._lightweight_claude_prompt(raw_line)
                        if provider == "claude"
                        else IncrementalLiveState._lightweight_codex_item(raw_line)
                    )
                    if record is None:
                        continue
                else:
                    try:
                        record = json.loads(raw_line)
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
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
                        result.latest_task = timestamp
                        result.task_tools.setdefault(timestamp, 0)
                    elif event_type == "task_complete":
                        result.completions.append(
                            (timestamp, turn_id if isinstance(turn_id, str) else None)
                        )
                if event_type == "item_completed" and result.latest_task is not None:
                    item = payload.get("item")
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("type"), str)
                        and item["type"] in CODEX_TOOL_ITEMS
                    ):
                        result.task_tools[result.latest_task] += 1
                if record_type == "compacted":
                    result.compactions.append(
                        {"timestamp": _iso(timestamp), "source": "codex_compacted"}
                    )
                    result.last_compacted = timestamp
                elif event_type == "context_compacted" and (
                    result.last_compacted is None
                    or not 0 <= timestamp - result.last_compacted <= 1
                ):
                    result.compactions.append(
                        {
                            "timestamp": _iso(timestamp),
                            "source": "codex_context_compacted",
                        }
                    )
    except (OSError, UnicodeError):
        pass
    return result, next_offset


def parse_telemetry(path: Path, provider: Provider) -> TranscriptTelemetry:
    """Incrementally cache sanitized metadata for one append-only transcript."""
    try:
        stat = path.stat()
    except OSError:
        return TranscriptTelemetry()
    key = provider, str(path)
    identity = stat.st_dev, stat.st_ino
    cached = _CACHE.get(key)
    if (
        cached is not None
        and cached.identity == identity
        and stat.st_size >= cached.size
    ):
        if cached.size == stat.st_size and cached.mtime_ns == stat.st_mtime_ns:
            return cached.telemetry
        parsed, offset = _read_telemetry(
            path, provider, cached.telemetry, cached.offset
        )
    else:
        parsed, offset = _read_telemetry(path, provider)
    _CACHE[key] = TelemetryCacheEntry(
        identity, offset, stat.st_size, stat.st_mtime_ns, parsed
    )
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
    session: dict[str, object],
    rows: list[TranscriptTelemetry],
    replace_forecast: bool,
) -> None:
    """Forecast from matching prompts, then sparse data from this session."""
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
    latest_timestamp = configurations[-1][0] if configurations else None
    latest_configurations = {
        (model, effort)
        for timestamp, model, effort in configurations
        if timestamp == latest_timestamp
    }
    latest_model, latest_effort = (
        next(iter(latest_configurations))
        if len(latest_configurations) == 1
        else (None, None)
    )
    latest_speed = session.get("speed")
    compact_events = session.get("compact_events")
    latest_compact = (
        max(
            (
                timestamp
                for event in compact_events
                if isinstance(event, dict)
                for timestamp in [_timestamp(event.get("timestamp"))]
                if timestamp is not None
            ),
            default=None,
        )
        if isinstance(compact_events, list)
        else None
    )
    if latest_effort is not None:
        session["reasoning_effort"] = latest_effort
    comparable = [
        iteration
        for iteration in iterations
        if latest_model is not None
        and latest_effort is not None
        and iteration.get("model") == latest_model
        and iteration.get("reasoning_effort") == latest_effort
        and iteration.get("speed") == latest_speed
        and (
            session.get("since_compact") is not True
            or latest_compact is None
            or (_timestamp(iteration.get("started_at")) or 0) > latest_compact
        )
        and iteration.get("completed") is True
        and iteration.get("priced") is True
        and _number(iteration.get("cost_usd")) is not None
    ][-FORECAST_WINDOW:]
    sufficient = len(comparable) >= FORECAST_MIN_SAMPLES
    if not replace_forecast:
        precompact_samples = sum(
            1
            for iteration in iterations
            for started_at in [_timestamp(iteration.get("started_at"))]
            if latest_compact is not None
            and started_at is not None
            and started_at <= latest_compact
            and iteration.get("priced") is True
        )
        forecast = session.get("projected_next_10_tasks_usd")
        if isinstance(forecast, (int, float)):
            session["forecast_basis"] = {
                "method": "context_scaled_precompact",
                "sample_count": precompact_samples,
                "model": None,
                "effort": None,
                "speed": None,
                "coverage": "fully_priced",
                "reason": None,
            }
            return
    if sufficient:
        session["forecast_basis"] = {
            "method": "same_config_completed_prompts",
            "sample_count": len(comparable),
            "model": latest_model,
            "effort": latest_effort,
            "speed": latest_speed,
            "coverage": "fully_priced",
            "reason": None,
        }
        session["projected_next_10_tasks_usd"] = round(
            sum(float(row["cost_usd"]) for row in comparable)
            / len(comparable)
            * FORECAST_WINDOW,
            6,
        )
        return
    recent_completed = [
        iteration
        for iteration in iterations
        if iteration.get("completed") is True
        and iteration.get("priced") is True
        and _number(iteration.get("cost_usd")) is not None
    ][-FORECAST_WINDOW:]
    if recent_completed:
        session["projected_next_10_tasks_usd"] = round(
            sum(float(row["cost_usd"]) for row in recent_completed)
            / len(recent_completed)
            * FORECAST_WINDOW,
            6,
        )
        session["forecast_basis"] = {
            "method": "sparse_session_prompts",
            "sample_count": len(recent_completed),
            "model": None,
            "effort": None,
            "speed": None,
            "coverage": "session_fallback",
            "reason": "no_provider_history",
        }
        return
    session["projected_next_10_tasks_usd"] = None
    session["forecast_basis"] = {
        "method": "unavailable",
        "sample_count": 0,
        "model": latest_model,
        "effort": latest_effort,
        "speed": latest_speed,
        "coverage": "insufficient_history",
        "reason": "no_completed_prompt_or_provider_history",
    }


def enrich_snapshot(
    snapshot: dict[str, object],
    claude_paths: list[Path],
    codex_paths: list[Path],
    now: float,
    provider_quotas: dict[str, object] | None = None,
) -> None:
    """Add explicit transcript telemetry without changing cost accounting."""
    grouped: dict[tuple[str, str], list[TranscriptTelemetry]] = {}
    sources: tuple[tuple[Provider, list[Path]], ...] = (
        ("claude", claude_paths),
        ("codex", codex_paths),
    )
    active_cache_keys = {
        (provider, str(path)) for provider, paths in sources for path in paths
    }
    for key in list(_CACHE):
        if key not in active_cache_keys:
            _CACHE.pop(key, None)
    for provider, paths in sources:
        for path in paths:
            row = parse_telemetry(path, provider)
            if row.session_id is not None and not row.is_subagent:
                grouped.setdefault((provider, row.session_id), []).append(row)
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
            session.get("forecast_mode") != "warming_up",
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
    account_quotas: dict[str, object] = {}
    if provider_quotas is not None:
        for provider in ("claude", "codex"):
            fresh = provider_quotas.get(provider)
            if isinstance(fresh, dict):
                account_quotas[provider] = fresh
    snapshot["account_quotas"] = account_quotas
