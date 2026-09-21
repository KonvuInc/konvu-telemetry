"""Incremental readers for active Claude and Codex transcripts."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Iterator

from .config import (
    ACTIVITY_FRESHNESS_SECONDS,
    DEFAULT_LIVE_WINDOW_SECONDS,
    MAX_PARSED_RECORD_BYTES,
)
from .models import Usage, UsageEvent
from .parsers import (
    assistant_event,
    codex_session_id,
    codex_subagent_parent,
    is_human_claude_prompt,
    session_title,
)
from .storage import (
    as_number,
    claude_roots,
    codex_roots,
    parse_timestamp,
    transcript_files,
    valid_session_id,
)


@dataclass
class ClaudeLiveFile:
    """Incremental state for one append-only Claude transcript."""

    offset: int = 0
    identity: tuple[int, int] | None = None
    session_id: str | None = None
    events: dict[str, UsageEvent] = field(default_factory=dict)
    first_timestamps: dict[str, float] = field(default_factory=dict)
    prompts: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    compacts: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    spawns: dict[tuple[str, str], float] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    clients: dict[str, str] = field(default_factory=dict)
    agent_id: str | None = None
    latest_assistant_at: float | None = None
    latest_stop_reason: str | None = None


@dataclass
class CodexLiveFile:
    """Incremental state for one append-only Codex rollout transcript."""

    offset: int = 0
    identity: tuple[int, int] | None = None
    session_id: str = ""
    events: list[UsageEvent] = field(default_factory=list)
    task_starts: list[float] = field(default_factory=list)
    tool_counts: dict[float, int] = field(default_factory=lambda: defaultdict(int))
    title: str | None = None
    client: str = "unknown"
    parent: tuple[str, str] | None = None
    previous_total: int | None = None
    previous_usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }
    )
    model: str = "unknown"
    speed: str = "standard"
    effort: str = "standard"
    latest_started_at: float = 0.0
    latest_terminal_at: float = 0.0


class IncrementalLiveState:
    """Read only appended transcript bytes while retaining compact accounting state."""

    def __init__(self) -> None:
        self.claude: dict[Path, ClaudeLiveFile] = {}
        self.codex: dict[Path, CodexLiveFile] = {}
        self._cold_discovered_codex_parents: set[str] = set()

    @staticmethod
    def _identity(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_dev, stat.st_ino

    @staticmethod
    def _codex_types(raw_line: bytes) -> list[str]:
        return [
            item.decode("utf-8", "replace")
            for item in re.findall(rb'(?<!\\)"type"\s*:\s*"([^"\\]+)"', raw_line[:8192])
        ]

    @staticmethod
    def _lightweight_codex_item(raw_line: bytes) -> dict[str, object] | None:
        """Keep accounting metadata from huge tool payloads without allocating their contents."""
        header = raw_line
        types = IncrementalLiveState._codex_types(header)
        timestamp = re.search(rb'"timestamp"\s*:\s*"([^"\\]+)"', header)
        if len(types) < 2 or timestamp is None:
            return None
        top_type, payload_type = types[:2]
        if top_type != "event_msg" or payload_type != "item_completed":
            return None
        item_type = types[2] if len(types) > 2 else ""
        return {
            "type": "event_msg",
            "timestamp": timestamp.group(1).decode("utf-8", "replace"),
            "payload": {"type": "item_completed", "item": {"type": item_type}},
        }

    @staticmethod
    def _lightweight_claude_prompt(raw_line: bytes) -> dict[str, object] | None:
        """Retain a huge human prompt's boundary without retaining its text."""
        header = raw_line
        types = IncrementalLiveState._codex_types(header)
        session_id = re.search(rb'"sessionId"\s*:\s*"([^"\\]+)"', header)
        timestamp = re.search(rb'"timestamp"\s*:\s*"([^"\\]+)"', header)
        if (
            not types
            or types[0] != "user"
            or len(types) > 1
            and types[1] == "tool_result"
            or session_id is None
            or timestamp is None
        ):
            return None
        decoded_session_id = session_id.group(1).decode("utf-8", "replace")
        if not valid_session_id(decoded_session_id):
            return None
        return {
            "sessionId": decoded_session_id,
            "timestamp": timestamp.group(1).decode("utf-8", "replace"),
            "message": {"role": "user", "content": [{"type": "text"}]},
            "isSidechain": b'"isSidechain":true' in header,
            "isMeta": b'"isMeta":true' in header,
            "isCompactSummary": b'"isCompactSummary":true' in header,
        }

    @staticmethod
    def _codex_needs_parse(raw_line: bytes) -> bool:
        types = IncrementalLiveState._codex_types(raw_line)
        if not types:
            return False
        if types[0] in {"session_meta", "turn_context"}:
            return True
        return (
            len(types) > 1
            and types[0] == "event_msg"
            and types[1]
            in {
                "thread_settings_applied",
                "user_message",
                "message",
                "task_started",
                "task_complete",
                "turn_aborted",
                "token_count",
            }
        )

    @staticmethod
    def _new_lines(
        path: Path, state: ClaudeLiveFile | CodexLiveFile
    ) -> Iterator[dict[str, object]]:
        identity = IncrementalLiveState._identity(path)
        if identity is None:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if state.identity != identity or size < state.offset:
            state.__dict__.update(type(state)().__dict__)
            state.identity = identity
            if isinstance(state, CodexLiveFile):
                state.session_id = codex_session_id(path)
        try:
            with path.open("rb") as transcript:
                transcript.seek(state.offset)
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
                                return
                            suffix = (suffix + remainder)[-MAX_PARSED_RECORD_BYTES:]
                            if remainder.endswith(b"\n"):
                                break
                        raw_line = prefix + suffix
                    state.offset = transcript.tell()
                    if isinstance(state, CodexLiveFile):
                        record = IncrementalLiveState._lightweight_codex_item(raw_line)
                        if record is not None:
                            yield record
                            continue
                        if oversized or not IncrementalLiveState._codex_needs_parse(
                            raw_line
                        ):
                            continue
                    elif oversized:
                        record = IncrementalLiveState._lightweight_claude_prompt(
                            raw_line
                        )
                        if record is not None:
                            yield record
                        continue
                    try:
                        record = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(record, dict):
                        yield record
        except OSError:
            return

    def _refresh_claude(self, path: Path) -> ClaudeLiveFile:
        state = self.claude.setdefault(path, ClaudeLiveFile())
        for record in self._new_lines(path, state):
            session_id = record.get("sessionId")
            if isinstance(session_id, str) and valid_session_id(session_id):
                state.session_id = session_id
            message = record.get("message")
            timestamp = parse_timestamp(record.get("timestamp"))
            if (
                isinstance(message, dict)
                and isinstance(session_id, str)
                and valid_session_id(session_id)
            ):
                if (
                    is_human_claude_prompt(record)
                    and timestamp is not None
                    and timestamp not in state.prompts[session_id]
                ):
                    state.prompts[session_id].append(timestamp)
                if (
                    record.get("isSidechain") is not True
                    and message.get("role") == "user"
                ):
                    title = session_title(message.get("content"))
                    if title:
                        state.titles.setdefault(session_id, title)
                if message.get("role") == "assistant" and timestamp is not None:
                    agent_id = record.get("agentId")
                    if isinstance(agent_id, str) and agent_id:
                        state.agent_id = agent_id
                    if (
                        state.latest_assistant_at is None
                        or timestamp >= state.latest_assistant_at
                    ):
                        state.latest_assistant_at = timestamp
                        stop_reason = message.get("stop_reason")
                        state.latest_stop_reason = (
                            stop_reason if isinstance(stop_reason, str) else None
                        )
            entrypoint = record.get("entrypoint")
            if (
                isinstance(session_id, str)
                and valid_session_id(session_id)
                and isinstance(entrypoint, str)
            ):
                if entrypoint == "claude-desktop":
                    state.clients[session_id] = "desktop"
                elif entrypoint == "cli" and session_id not in state.clients:
                    state.clients[session_id] = "cli"
                elif entrypoint == "sdk-cli" and session_id not in state.clients:
                    state.clients[session_id] = "sdk"
            if (
                record.get("type") == "system"
                and record.get("subtype") == "compact_boundary"
                and isinstance(session_id, str)
                and timestamp is not None
            ):
                if timestamp not in state.compacts[session_id]:
                    state.compacts[session_id].append(timestamp)
            tool_result = record.get("toolUseResult")
            if (
                isinstance(tool_result, dict)
                and isinstance(session_id, str)
                and timestamp is not None
            ):
                agent_id = tool_result.get("agentId")
                if isinstance(agent_id, str) and agent_id:
                    state.spawns[(session_id, agent_id)] = timestamp
            elif (
                isinstance(message, dict)
                and record.get("isSidechain") is True
                and isinstance(session_id, str)
                and timestamp is not None
                and message.get("role") == "user"
            ):
                agent_id = record.get("agentId")
                if isinstance(agent_id, str) and agent_id:
                    state.spawns.setdefault((session_id, agent_id), timestamp)
            event = assistant_event(record)
            if event is not None and event.message_id is not None:
                first_timestamp = state.first_timestamps.setdefault(
                    event.message_id, event.timestamp
                )
                state.events[event.message_id] = (
                    event
                    if event.timestamp == first_timestamp
                    else UsageEvent(
                        event.provider,
                        event.session_id,
                        event.message_id,
                        first_timestamp,
                        event.model,
                        event.usage,
                        event.tool_calls,
                        event.is_subagent,
                        event.agent_id,
                        event.effort,
                        event.context_window_tokens,
                    )
                )
        return state

    def _refresh_codex(self, path: Path) -> CodexLiveFile:
        state = self.codex.setdefault(
            path, CodexLiveFile(session_id=codex_session_id(path))
        )
        for record in self._new_lines(path, state):
            payload = record.get("payload")
            timestamp = parse_timestamp(record.get("timestamp"))
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "session_meta":
                recorded_id = payload.get("id")
                if recorded_id != state.session_id:
                    continue
                source = payload.get("source")
                originator = payload.get("originator")
                if source == "vscode" or originator == "Codex Desktop":
                    state.client = "desktop"
                elif source == "cli" or originator == "codex-tui":
                    state.client = "cli"
                subagent = source.get("subagent") if isinstance(source, dict) else None
                if isinstance(subagent, dict):
                    spawned = subagent.get("thread_spawn")
                    parent = (
                        spawned.get("parent_thread_id")
                        if isinstance(spawned, dict)
                        else payload.get("parent_thread_id")
                    )
                    path_label = (
                        spawned.get("agent_path") if isinstance(spawned, dict) else None
                    )
                    nickname = (
                        spawned.get("agent_nickname")
                        if isinstance(spawned, dict)
                        else subagent.get("other")
                    )
                    label = (
                        nickname
                        if isinstance(nickname, str) and nickname
                        else path_label
                        if isinstance(path_label, str) and path_label
                        else "subagent"
                    )
                    if (
                        isinstance(parent, str)
                        and valid_session_id(parent)
                        and parent != state.session_id
                    ):
                        state.parent = (parent, label)
                continue
            if record.get("type") == "turn_context":
                model = payload.get("model")
                if isinstance(model, str) and model:
                    state.model = model
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
                state.effort = str(effort or configured_effort or state.effort)
                continue
            if record.get("type") != "event_msg":
                continue
            event_type = payload.get("type")
            if event_type == "thread_settings_applied":
                settings = payload.get("thread_settings")
                tier = (
                    settings.get("service_tier") if isinstance(settings, dict) else None
                )
                state.speed = (
                    "flex"
                    if tier == "flex"
                    else "fast"
                    if tier in {"fast", "priority"}
                    else "standard"
                )
            if event_type in {"user_message", "message"}:
                role = payload.get("role")
                if event_type == "user_message" or role == "user":
                    title = session_title(
                        payload.get("message")
                        if event_type == "user_message"
                        else payload.get("content")
                    )
                    if title:
                        state.title = state.title or title
            if timestamp is not None and event_type == "task_started":
                if timestamp not in state.task_starts:
                    state.task_starts.append(timestamp)
                    state.task_starts.sort()
                state.latest_started_at = max(state.latest_started_at, timestamp)
            elif timestamp is not None and event_type in {
                "task_complete",
                "turn_aborted",
            }:
                state.latest_terminal_at = max(state.latest_terminal_at, timestamp)
            if event_type == "item_completed" and timestamp is not None:
                item = payload.get("item")
                tool_items = {
                    "CommandExecution",
                    "McpToolCall",
                    "WebSearch",
                    "ViewImageToolCall",
                    "ImageGeneration",
                }
                if isinstance(item, dict) and item.get("type") in tool_items:
                    index = bisect_right(state.task_starts, timestamp) - 1
                    if index >= 0:
                        state.tool_counts[state.task_starts[index]] += 1
            if event_type != "token_count" or timestamp is None:
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            total_usage = info.get("total_token_usage")
            last_usage = info.get("last_token_usage")
            if not isinstance(total_usage, dict):
                continue
            cumulative = as_number(total_usage.get("total_tokens"))
            if state.previous_total is not None and cumulative == state.previous_total:
                continue
            state.previous_total = cumulative
            raw = (
                last_usage
                if isinstance(last_usage, dict)
                else {
                    key: max(
                        0, as_number(total_usage.get(key)) - state.previous_usage[key]
                    )
                    for key in state.previous_usage
                }
            )
            for key in state.previous_usage:
                state.previous_usage[key] = as_number(total_usage.get(key))
            input_tokens = as_number(raw.get("input_tokens"))
            cached_input_tokens = as_number(raw.get("cached_input_tokens"))
            cache_write_tokens = min(
                as_number(raw.get("cache_write_input_tokens")),
                max(0, input_tokens - cached_input_tokens),
            )
            model = str(info.get("model") or info.get("model_name") or state.model)
            context_window = as_number(info.get("model_context_window"))
            state.events.append(
                UsageEvent(
                    "codex",
                    state.session_id,
                    f"{state.session_id}:{cumulative}:{timestamp}",
                    timestamp,
                    model,
                    Usage(
                        max(0, input_tokens - cached_input_tokens - cache_write_tokens),
                        as_number(raw.get("output_tokens")),
                        cache_write_tokens,
                        0,
                        cached_input_tokens,
                        0,
                        state.speed,
                        as_number(raw.get("reasoning_output_tokens")),
                    ),
                    0,
                    False,
                    None,
                    state.effort,
                    context_window or None,
                )
            )
        return state

    def refresh(self, now: float) -> tuple[list[Path], list[Path]]:
        """Discover live roots cheaply, then parse only bytes appended since the last poll."""
        claude_paths: set[Path] = set()
        for root in claude_roots():
            if not root.is_dir():
                continue
            transcripts = list(transcript_files(root))
            try:
                resolved_root = root.resolve(strict=True)
            except OSError:
                continue
            root_transcripts: dict[str, Path] = {}
            child_transcripts: dict[str, set[Path]] = {}
            for transcript in transcripts:
                try:
                    relative = transcript.resolve(strict=True).relative_to(
                        resolved_root
                    )
                    if len(relative.parts) == 2:
                        root_transcripts[transcript.stem] = transcript
                    elif len(relative.parts) > 2:
                        child_transcripts.setdefault(relative.parts[1], set()).add(
                            transcript
                        )
                except OSError:
                    continue
                except ValueError:
                    continue
            active_ids: set[str] = set()
            for transcript in transcripts:
                try:
                    if now - transcript.stat().st_mtime > DEFAULT_LIVE_WINDOW_SECONDS:
                        continue
                except OSError:
                    continue
                state = self._refresh_claude(transcript)
                if state.session_id:
                    active_ids.add(state.session_id)
            for session_id in active_ids:
                root_transcript = root_transcripts.get(session_id)
                if root_transcript is None:
                    continue
                claude_paths.add(root_transcript)
                claude_paths.update(child_transcripts.get(session_id, ()))
        for path in claude_paths:
            self._refresh_claude(path)
        codex_paths: list[Path] = []
        all_codex_paths: list[Path] = []
        for root in codex_roots():
            if not root.is_dir():
                continue
            transcripts = list(transcript_files(root))
            all_codex_paths.extend(transcripts)
            for transcript in transcripts:
                try:
                    if now - transcript.stat().st_mtime <= DEFAULT_LIVE_WINDOW_SECONDS:
                        codex_paths.append(transcript)
                except OSError:
                    continue
        for path in codex_paths:
            self._refresh_codex(path)
        active_parent_ids = {
            state.session_id
            for path, state in self.codex.items()
            if path in codex_paths and state.parent is None
        }
        undiscovered_parents = active_parent_ids - self._cold_discovered_codex_parents
        if undiscovered_parents:
            relationships = {
                path: parent
                for path in all_codex_paths
                if path not in codex_paths
                for parent in [codex_subagent_parent(path)]
                if parent is not None
            }
            pending = set(undiscovered_parents)
            discovered_paths: set[Path] = set()
            while pending:
                parent_id = pending.pop()
                for path, parent in relationships.items():
                    if path in discovered_paths or parent[0] != parent_id:
                        continue
                    discovered_paths.add(path)
                    pending.add(codex_session_id(path))
            for path in sorted(discovered_paths):
                self._refresh_codex(path)
                codex_paths.append(path)
            self._cold_discovered_codex_parents.update(active_parent_ids)
        retained_children = {
            path
            for path, state in self.codex.items()
            if state.parent is not None and state.parent[0] in active_parent_ids
        }
        for path in retained_children:
            if path not in codex_paths:
                self._refresh_codex(path)
                codex_paths.append(path)
        active = set(claude_paths) | set(codex_paths)
        self.claude = {
            path: state for path, state in self.claude.items() if path in active
        }
        self.codex = {
            path: state for path, state in self.codex.items() if path in active
        }
        return sorted(claude_paths), sorted(codex_paths)

    def claude_statuses(self, root_transcript: Path, now: float) -> dict[str, bool]:
        prefix = root_transcript.parent / root_transcript.stem / "subagents"
        statuses: dict[str, bool] = {}
        for path, state in self.claude.items():
            if (
                prefix not in path.parents
                or state.agent_id is None
                or state.latest_assistant_at is None
            ):
                continue
            statuses[state.agent_id] = (
                now - state.latest_assistant_at <= ACTIVITY_FRESHNESS_SECONDS
                and state.latest_stop_reason != "end_turn"
            )
        return statuses
