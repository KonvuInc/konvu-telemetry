"""Provider transcript parsers and live transcript discovery."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
import json
from pathlib import Path
import re
from typing import Iterator

from .config import ACTIVITY_FRESHNESS_SECONDS, DEFAULT_LIVE_WINDOW_SECONDS
from .models import Usage, UsageEvent
from .storage import (
    as_number,
    claude_roots,
    codex_roots,
    file_cached,
    file_cached_list,
    parse_timestamp,
    root_claude_transcripts,
    transcript_files,
    valid_session_id,
)


def has_usage_fields(raw: dict[str, object], *fields: str) -> bool:
    """Return whether a provider record explicitly supplied each required token field."""
    for field in fields:
        value = raw.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        if value < 0:
            return False
    return True


def is_human_claude_prompt(record: dict[str, object]) -> bool:
    """Identify real root-user prompts shared by cold and incremental parsing."""
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
        isinstance(item, dict) and item.get("type") in {"text", "image", "document"}
        for item in content
    )


def assistant_event(record: dict[str, object]) -> UsageEvent | None:
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    raw_usage = message.get("usage")
    session_id = record.get("sessionId")
    timestamp = parse_timestamp(record.get("timestamp"))
    if (
        not isinstance(raw_usage, dict)
        or not isinstance(session_id, str)
        or timestamp is None
    ):
        return None
    if not valid_session_id(session_id):
        return None
    content = message.get("content")
    tool_calls = 0
    if isinstance(content, list):
        tool_calls = sum(
            1
            for item in content
            if isinstance(item, dict) and item.get("type") == "tool_use"
        )
    cache_creation = raw_usage.get("cache_creation")
    one_hour_cache_write = 0
    five_minute_cache_write = 0
    if isinstance(cache_creation, dict):
        one_hour_cache_write = as_number(
            cache_creation.get("ephemeral_1h_input_tokens")
        )
        five_minute_cache_write = as_number(
            cache_creation.get("ephemeral_5m_input_tokens")
        )
    cache_write_tokens = max(
        as_number(raw_usage.get("cache_creation_input_tokens")),
        one_hour_cache_write + five_minute_cache_write,
    )
    server_tool_use = raw_usage.get("server_tool_use")
    web_search_requests = (
        as_number(server_tool_use.get("web_search_requests"))
        if isinstance(server_tool_use, dict)
        else 0
    )
    usage = Usage(
        input_tokens=as_number(raw_usage.get("input_tokens")),
        output_tokens=as_number(raw_usage.get("output_tokens")),
        cache_write_tokens=cache_write_tokens,
        cache_write_one_hour_tokens=min(one_hour_cache_write, cache_write_tokens),
        cache_read_tokens=as_number(raw_usage.get("cache_read_input_tokens")),
        web_search_requests=web_search_requests,
        speed=str(raw_usage.get("speed") or "standard"),
        complete=has_usage_fields(raw_usage, "input_tokens", "output_tokens"),
    )
    model = str(message.get("model") or "unknown")
    if (
        model == "<synthetic>"
        and usage.total_tokens == 0
        and usage.web_search_requests == 0
    ):
        return None
    raw_agent_id = record.get("agentId")
    return UsageEvent(
        provider="claude",
        session_id=session_id,
        message_id=message.get("id") if isinstance(message.get("id"), str) else None,
        timestamp=timestamp,
        model=model,
        usage=usage,
        tool_calls=tool_calls,
        is_subagent=record.get("isSidechain") is True,
        agent_id=raw_agent_id if isinstance(raw_agent_id, str) else None,
        effort=str(record.get("effort") or "standard"),
    )


@file_cached_list
def events_in_file(file_path: Path) -> Iterator[UsageEvent]:
    """Deduplicate streamed Claude records by retaining each message's final record."""
    records: list[dict[str, object]] = []
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return
    first_timestamp_by_id: dict[str, object] = {}
    last_index_by_id: dict[str, int] = {}
    for index, record in enumerate(records):
        message = record.get("message")
        message_id = message.get("id") if isinstance(message, dict) else None
        if not isinstance(message_id, str):
            continue
        first_timestamp_by_id.setdefault(message_id, record.get("timestamp"))
        last_index_by_id[message_id] = index
    for index, record in enumerate(records):
        message = record.get("message")
        message_id = message.get("id") if isinstance(message, dict) else None
        if isinstance(message_id, str):
            if last_index_by_id[message_id] != index:
                continue
            if record.get("timestamp") != first_timestamp_by_id[message_id]:
                record = {**record, "timestamp": first_timestamp_by_id[message_id]}
        event = assistant_event(record)
        if event is not None:
            yield event


def codex_session_id(file_path: Path) -> str:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        file_path.name,
    )
    return match.group(1) if match else file_path.stem


@file_cached_list
def codex_events_in_file(file_path: Path) -> Iterator[UsageEvent]:
    """Normalize Codex cumulative token checkpoints into per-call usage events."""
    previous_total: int | None = None
    previous = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    session_id = codex_session_id(file_path)
    session_model = "unknown"
    session_speed = "standard"
    session_effort = "standard"
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if record.get("type") == "turn_context" and isinstance(payload, dict):
                    model = payload.get("model")
                    if isinstance(model, str) and model:
                        session_model = model
                    effort = payload.get("effort")
                    collaboration = payload.get("collaboration_mode")
                    settings = (
                        collaboration.get("settings")
                        if isinstance(collaboration, dict)
                        else None
                    )
                    configured_effort = (
                        settings.get("reasoning_effort")
                        if isinstance(settings, dict)
                        else None
                    )
                    session_effort = str(effort or configured_effort or session_effort)
                if (
                    record.get("type") == "event_msg"
                    and isinstance(payload, dict)
                    and payload.get("type") == "thread_settings_applied"
                ):
                    settings = payload.get("thread_settings")
                    tier = (
                        settings.get("service_tier")
                        if isinstance(settings, dict)
                        else None
                    )
                    session_speed = (
                        "flex"
                        if tier == "flex"
                        else "fast"
                        if isinstance(tier, str) and tier in {"fast", "priority"}
                        else "standard"
                    )
                if record.get("type") != "event_msg":
                    continue
                if (
                    not isinstance(payload, dict)
                    or payload.get("type") != "token_count"
                ):
                    continue
                info = payload.get("info")
                timestamp = parse_timestamp(record.get("timestamp"))
                if not isinstance(info, dict) or timestamp is None:
                    continue
                total_usage = info.get("total_token_usage")
                last_usage = info.get("last_token_usage")
                if not isinstance(total_usage, dict):
                    continue
                cumulative = as_number(total_usage.get("total_tokens"))
                if previous_total is not None and cumulative == previous_total:
                    continue
                previous_total = cumulative
                raw = (
                    last_usage
                    if isinstance(last_usage, dict)
                    else {
                        key: max(0, as_number(total_usage.get(key)) - previous[key])
                        for key in previous
                    }
                )
                for key in previous:
                    previous[key] = as_number(total_usage.get(key))
                usage_source = (
                    last_usage if isinstance(last_usage, dict) else total_usage
                )
                input_tokens = as_number(raw.get("input_tokens"))
                cached_input_tokens = as_number(raw.get("cached_input_tokens"))
                cache_write_tokens = min(
                    as_number(raw.get("cache_write_input_tokens")),
                    max(0, input_tokens - cached_input_tokens),
                )
                model = str(
                    info.get("model") or info.get("model_name") or session_model
                )
                context_window = as_number(info.get("model_context_window"))
                yield UsageEvent(
                    provider="codex",
                    session_id=session_id,
                    message_id=f"{session_id}:{cumulative}",
                    timestamp=timestamp,
                    model=model,
                    usage=Usage(
                        input_tokens=max(
                            0, input_tokens - cached_input_tokens - cache_write_tokens
                        ),
                        output_tokens=as_number(raw.get("output_tokens")),
                        cache_write_tokens=cache_write_tokens,
                        cache_write_one_hour_tokens=0,
                        cache_read_tokens=cached_input_tokens,
                        web_search_requests=0,
                        speed=session_speed,
                        reasoning_output_tokens=as_number(
                            raw.get("reasoning_output_tokens")
                        ),
                        complete=has_usage_fields(
                            usage_source, "input_tokens", "output_tokens"
                        ),
                    ),
                    tool_calls=0,
                    is_subagent=False,
                    agent_id=None,
                    effort=session_effort,
                    context_window_tokens=context_window or None,
                )
    except OSError:
        return


@file_cached
def codex_task_starts(file_path: Path) -> list[float]:
    """Read recorded task boundaries, falling back to real user-message timestamps."""
    starts: list[float] = []
    fallback_starts: list[float] = []
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict) or record.get("type") != "event_msg":
                    continue
                timestamp = parse_timestamp(record.get("timestamp"))
                if timestamp is None:
                    continue
                if payload.get("type") == "task_started":
                    starts.append(timestamp)
                elif payload.get("type") == "user_message" or (
                    payload.get("type") == "message" and payload.get("role") == "user"
                ):
                    fallback_starts.append(timestamp)
    except OSError:
        return starts
    return sorted(set(starts or fallback_starts))


@file_cached
def codex_task_tool_counts(file_path: Path) -> dict[float, int]:
    """Count completed tools inside each recorded Codex task."""
    starts = codex_task_starts(file_path)
    counts: dict[float, int] = defaultdict(int)
    if not starts:
        return counts
    tool_items = {
        "CommandExecution",
        "McpToolCall",
        "WebSearch",
        "ViewImageToolCall",
        "ImageGeneration",
    }
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                if (
                    not isinstance(payload, dict)
                    or record.get("type") != "event_msg"
                    or payload.get("type") != "item_completed"
                ):
                    continue
                item = payload.get("item")
                timestamp = parse_timestamp(record.get("timestamp"))
                if (
                    not isinstance(item, dict)
                    or item.get("type") not in tool_items
                    or timestamp is None
                ):
                    continue
                task_index = bisect_right(starts, timestamp) - 1
                if task_index >= 0:
                    counts[starts[task_index]] += 1
    except OSError:
        return counts
    return counts


def codex_turn_tool_calls(file_path: Path, turn_id: str) -> int:
    """Count completed tool calls belonging to one exact Codex turn."""
    tool_items = {
        "CommandExecution",
        "McpToolCall",
        "WebSearch",
        "ViewImageToolCall",
        "ImageGeneration",
    }
    count = 0
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict) or record.get("type") != "event_msg":
                    continue
                if (
                    payload.get("type") != "item_completed"
                    or payload.get("turn_id") != turn_id
                ):
                    continue
                item = payload.get("item")
                if isinstance(item, dict) and item.get("type") in tool_items:
                    count += 1
    except OSError:
        return 0
    return count


def codex_hook_transcript(payload: dict[str, object], session_id: str) -> Path | None:
    """Locate the transcript for the exact Codex hook invocation."""
    if not valid_session_id(session_id):
        return None
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str):
        candidate = Path(transcript_path).expanduser()
        for root in codex_roots():
            if (
                candidate in set(transcript_files(root))
                and codex_session_id(candidate) == session_id
            ):
                return candidate
    for root in codex_roots():
        candidates = sorted(
            (
                path
                for path in transcript_files(root)
                if codex_session_id(path) == session_id
            ),
            key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def latest_user_prompt_by_session(file_path: Path) -> dict[str, float]:
    return {
        session_id: timestamps[-1]
        for session_id, timestamps in user_prompt_times_by_session(file_path).items()
        if timestamps
    }


def text_content(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
        return " ".join(texts) if texts else None
    return None


def session_title(content: object) -> str | None:
    """Turn a user prompt into a short, safe dashboard label."""
    raw_text = text_content(content)
    if raw_text is None:
        return None
    collapsed = " ".join(raw_text.split())
    ignored_prefixes = (
        "<",
        "# AGENTS.md instructions",
        "The following is the Codex agent history",
        "MANDATORY — NON-NEGOTIABLE",
    )
    if not collapsed or collapsed.startswith(ignored_prefixes):
        return None
    return collapsed[:88]


@file_cached
def claude_titles_in_file(file_path: Path) -> dict[str, str]:
    """Return the first human prompt for each root Claude session."""
    titles: dict[str, str] = {}
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("isSidechain") is True:
                    continue
                message = record.get("message")
                session_id = record.get("sessionId")
                if (
                    not isinstance(message, dict)
                    or message.get("role") != "user"
                    or not isinstance(session_id, str)
                ):
                    continue
                title = session_title(message.get("content"))
                if title:
                    titles.setdefault(session_id, title)
    except OSError:
        return titles
    return titles


@file_cached
def claude_client_in_file(file_path: Path) -> dict[str, str]:
    """Read Claude's recorded entrypoint instead of guessing its client."""
    clients: dict[str, str] = {}
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                session_id = record.get("sessionId")
                entrypoint = record.get("entrypoint")
                if not isinstance(session_id, str) or not isinstance(entrypoint, str):
                    continue
                if entrypoint == "claude-desktop":
                    clients[session_id] = "desktop"
                elif entrypoint == "cli" and session_id not in clients:
                    clients[session_id] = "cli"
                elif entrypoint == "sdk-cli" and session_id not in clients:
                    clients[session_id] = "sdk"
    except OSError:
        return clients
    return clients


@file_cached
def codex_title_in_file(file_path: Path) -> str | None:
    """Return the first human prompt from one Codex rollout transcript."""
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict):
                    continue
                is_legacy_user = payload.get("type") == "user_message"
                is_response_user = (
                    payload.get("type") == "message" and payload.get("role") == "user"
                )
                if not is_legacy_user and not is_response_user:
                    continue
                title = session_title(
                    payload.get("message") if is_legacy_user else payload.get("content")
                )
                if title:
                    return title
    except OSError:
        return None
    return None


@file_cached
def codex_client_in_file(file_path: Path) -> str:
    """Read Codex's origin metadata to distinguish Desktop from the TUI."""
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if record.get("type") != "session_meta" or not isinstance(
                    payload, dict
                ):
                    continue
                source = payload.get("source")
                originator = payload.get("originator")
                if source == "vscode" or originator == "Codex Desktop":
                    return "desktop"
                if source == "cli" or originator == "codex-tui":
                    return "cli"
    except OSError:
        return "unknown"
    return "unknown"


def codex_subagent_parent(file_path: Path) -> tuple[str, str] | None:
    """Return the parent session and readable task path for a spawned Codex agent."""
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    return None
                source = payload.get("source")
                subagent = source.get("subagent") if isinstance(source, dict) else None
                if not isinstance(subagent, dict):
                    return None
                spawned = subagent.get("thread_spawn")
                if isinstance(spawned, dict):
                    parent = spawned.get("parent_thread_id")
                    path = spawned.get("agent_path")
                    nickname = spawned.get("agent_nickname")
                    label = (
                        nickname
                        if isinstance(nickname, str) and nickname
                        else path
                        if isinstance(path, str) and path
                        else "subagent"
                    )
                else:
                    # Codex's own review agents carry no thread_spawn block; the parent sits on the meta itself.
                    parent = payload.get("parent_thread_id")
                    other = subagent.get("other")
                    label = other if isinstance(other, str) and other else "subagent"
                own_id = codex_session_id(file_path)
                return (
                    (parent, label)
                    if isinstance(parent, str) and parent != own_id
                    else None
                )
    except OSError:
        return None
    return None


def codex_subagent_is_live(file_path: Path, now: float) -> bool:
    """Treat a child as live only while its latest task has no terminal event."""
    latest_started = 0.0
    latest_terminal = 0.0
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                timestamp = parse_timestamp(record.get("timestamp"))
                event_type = payload.get("type") if isinstance(payload, dict) else None
                if timestamp is None:
                    continue
                if event_type == "task_started":
                    latest_started = max(latest_started, timestamp)
                elif event_type in {"task_complete", "turn_aborted"}:
                    latest_terminal = max(latest_terminal, timestamp)
    except OSError:
        return False
    return (
        latest_started > latest_terminal
        and now - latest_started <= ACTIVITY_FRESHNESS_SECONDS
    )


@file_cached
def user_prompt_times_by_session(file_path: Path) -> dict[str, list[float]]:
    """Return root prompt boundaries, excluding sidechain/subagent messages."""
    prompts: dict[str, list[float]] = defaultdict(list)
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or not is_human_claude_prompt(record):
                    continue
                session_id = record.get("sessionId")
                timestamp = parse_timestamp(record.get("timestamp"))
                if isinstance(session_id, str) and timestamp is not None:
                    prompts[session_id].append(timestamp)
    except OSError:
        return prompts
    return {
        session_id: sorted(set(timestamps))
        for session_id, timestamps in prompts.items()
    }


@file_cached
def spawned_agent_times(file_path: Path) -> dict[tuple[str, str], float]:
    """Read Claude child spawn times, including workflow child assignments."""
    spawned: dict[tuple[str, str], float] = {}
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                tool_result = record.get("toolUseResult")
                session_id = record.get("sessionId")
                timestamp = parse_timestamp(record.get("timestamp"))
                if not isinstance(session_id, str) or timestamp is None:
                    continue
                if isinstance(tool_result, dict):
                    agent_id = tool_result.get("agentId")
                    if isinstance(agent_id, str) and agent_id:
                        spawned[(session_id, agent_id)] = timestamp
                    continue
                message = record.get("message")
                agent_id = record.get("agentId")
                if (
                    record.get("isSidechain") is True
                    and isinstance(agent_id, str)
                    and isinstance(message, dict)
                    and message.get("role") == "user"
                ):
                    spawned.setdefault((session_id, agent_id), timestamp)
    except OSError:
        return spawned
    return spawned


@file_cached
def spawned_agent_labels(file_path: Path) -> dict[tuple[str, str], str]:
    """Return each Claude subagent's task description or initial assignment."""
    labels: dict[tuple[str, str], str] = {}
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                session_id = record.get("sessionId")
                if not isinstance(session_id, str):
                    continue
                tool_result = record.get("toolUseResult")
                if isinstance(tool_result, dict):
                    agent_id = tool_result.get("agentId")
                    label = session_title(tool_result.get("description"))
                    if isinstance(agent_id, str) and label:
                        labels[(session_id, agent_id)] = label
                    continue
                message = record.get("message")
                agent_id = record.get("agentId")
                if (
                    record.get("isSidechain") is not True
                    or not isinstance(agent_id, str)
                    or not isinstance(message, dict)
                    or message.get("role") != "user"
                ):
                    continue
                label = session_title(message.get("content"))
                if label:
                    labels.setdefault((session_id, agent_id), label)
    except OSError:
        return labels
    return labels


def claude_subagent_statuses(root_transcript: Path, now: float) -> dict[str, bool]:
    """Return whether each Claude child transcript is still executing."""
    statuses: dict[str, bool] = {}
    subagents_directory = root_transcript.parent / root_transcript.stem / "subagents"
    if not subagents_directory.is_dir():
        return statuses
    for root in claude_roots():
        transcript_paths = [
            path
            for path in transcript_files(root)
            if subagents_directory in path.parents and path.name.startswith("agent-")
        ]
        if transcript_paths:
            break
    else:
        transcript_paths = []
    for transcript_path in transcript_paths:
        agent_id: str | None = None
        latest_timestamp: float | None = None
        latest_stop_reason: str | None = None
        try:
            with transcript_path.open(
                "r", encoding="utf-8", errors="replace"
            ) as transcript:
                for line in transcript:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    record_agent_id = record.get("agentId")
                    if isinstance(record_agent_id, str) and record_agent_id:
                        agent_id = record_agent_id
                    message = record.get("message")
                    timestamp = parse_timestamp(record.get("timestamp"))
                    if (
                        not isinstance(message, dict)
                        or message.get("role") != "assistant"
                        or timestamp is None
                    ):
                        continue
                    if latest_timestamp is None or timestamp >= latest_timestamp:
                        latest_timestamp = timestamp
                        stop_reason = message.get("stop_reason")
                        latest_stop_reason = (
                            stop_reason if isinstance(stop_reason, str) else None
                        )
        except OSError:
            continue
        if agent_id is None or latest_timestamp is None:
            continue
        statuses[agent_id] = (
            now - latest_timestamp <= ACTIVITY_FRESHNESS_SECONDS
            and latest_stop_reason != "end_turn"
        )
    return statuses


@file_cached
def transcript_session_id(file_path: Path) -> str | None:
    """Read the root Claude session id carried by every transcript record."""
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = (
                    record.get("sessionId") if isinstance(record, dict) else None
                )
                if valid_session_id(session_id):
                    return session_id
    except OSError:
        return None
    return None


def claude_hook_transcript(payload: dict[str, object], session_id: str) -> Path | None:
    """Locate the Claude transcript bound to the claimed hook session."""
    if not valid_session_id(session_id):
        return None
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str):
        candidate = Path(transcript_path).expanduser()
        for root in claude_roots():
            if (
                candidate in set(transcript_files(root))
                and transcript_session_id(candidate) == session_id
            ):
                return candidate
    for root in claude_roots():
        candidates = sorted(
            (
                path
                for path in transcript_files(root)
                if transcript_session_id(path) == session_id
            ),
            key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def live_transcripts(now: float) -> list[Path]:
    active_session_ids: set[str] = set()
    transcripts_by_path: set[Path] = set()
    for root in claude_roots():
        if not root.is_dir():
            continue
        for transcript in transcript_files(root):
            try:
                if now - transcript.stat().st_mtime <= DEFAULT_LIVE_WINDOW_SECONDS:
                    session_id = transcript_session_id(transcript)
                    if session_id:
                        active_session_ids.add(session_id)
            except OSError:
                continue
        for session_id in active_session_ids:
            root_transcript = next(
                (
                    path
                    for path in root_claude_transcripts(root)
                    if path.name == f"{session_id}.jsonl"
                ),
                None,
            )
            if root_transcript is None:
                continue
            transcripts_by_path.add(root_transcript)
            session_directory = root_transcript.parent / session_id
            transcripts_by_path.update(
                path
                for path in transcript_files(root)
                if session_directory in path.parents
            )
    return sorted(transcripts_by_path)


def live_codex_transcripts(now: float) -> list[Path]:
    transcripts: list[Path] = []
    for root in codex_roots():
        if not root.is_dir():
            continue
        for transcript in transcript_files(root):
            try:
                if now - transcript.stat().st_mtime <= DEFAULT_LIVE_WINDOW_SECONDS:
                    transcripts.append(transcript)
            except OSError:
                continue
    return transcripts
