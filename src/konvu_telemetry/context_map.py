"""Incrementally derive privacy-preserving context events from local transcripts."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import TypedDict, cast
from urllib.parse import urlparse

from .context_tokenizer import ContextTokenizer
from .live import IncrementalLiveState
from .parsers import is_human_claude_prompt
from .storage import context_map_path, parse_timestamp, write_private_json_if_changed


STATE_VERSION = 18
MAX_OUTPUT_EXCERPT_RANGES = 8
# Claude attachments that restate session settings on every turn; they never go stale.
SESSION_SETTING_ATTACHMENTS = {
    "prompt_snapshot",
    "date",
    "model",
    "auto_mode",
    "session_context",
    "agent_listing_delta",
    "command_permissions",
    "remote_session_change",
}
MAX_CONTEXT_RECORD_BYTES = 8 * 1024 * 1024
CLAUDE_RESULT_FRAME_TOKENS = {"3.0": 40, "5.0": 23}
CODEX_RESULT_FRAME_TOKENS = 10
LOGGER = logging.getLogger(__name__)


class Record(TypedDict):
    start: int
    end: int
    size: int
    digest: str
    oversized: bool
    value: dict[str, object] | None


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _integer(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _number(value: object) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def _record_stream(path: Path, offset: int) -> Iterator[Record]:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                start = handle.tell()
                line = handle.readline(MAX_CONTEXT_RECORD_BYTES + 1)
                if not line:
                    return
                digest = hashlib.sha256()
                digest.update(line)
                size = len(line)
                if not line.endswith(b"\n"):
                    if size <= MAX_CONTEXT_RECORD_BYTES:
                        return
                    while True:
                        chunk = handle.readline(MAX_CONTEXT_RECORD_BYTES + 1)
                        if not chunk:
                            return
                        digest.update(chunk)
                        size += len(chunk)
                        if chunk.endswith(b"\n"):
                            break
                    yield {
                        "start": start,
                        "end": handle.tell(),
                        "size": size,
                        "digest": digest.hexdigest(),
                        "oversized": True,
                        "value": None,
                    }
                    continue
                try:
                    parsed = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = None
                yield {
                    "start": start,
                    "end": handle.tell(),
                    "size": size,
                    "digest": digest.hexdigest(),
                    "oversized": False,
                    "value": parsed if isinstance(parsed, dict) else None,
                }
    except OSError:
        return


def _safe_label(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.netloc}{parsed.path[:80]}"
    if "/" in value or "\\" in value:
        parts = value.replace("\\", "/").split("/")
        return "/".join(part for part in parts[-3:] if part)[:120]
    return value[:120]


def _nested_tool_name(arguments: object) -> str:
    if isinstance(arguments, str):
        match = re.search(r"tools\.([A-Za-z0-9_]+)", arguments)
        return match.group(1)[:120] if match else ""
    return ""


def _argument_label(arguments: object) -> str:
    nested = _nested_tool_name(arguments)
    if nested:
        return nested.replace("__", " · ")
    if not isinstance(arguments, dict):
        return ""
    for key in ("file_path", "filename", "path", "url", "uri"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _safe_label(value)
    return ""


def _argument_hints(arguments: object) -> str:
    if isinstance(arguments, str):
        return arguments[:500]
    if not isinstance(arguments, dict):
        return ""
    return " ".join(
        value[:300]
        for key in (
            "command",
            "cmd",
            "code",
            "url",
            "uri",
            "file_path",
            "filename",
            "path",
            "server",
        )
        for value in [arguments.get(key)]
        if isinstance(value, str)
    )


def _shell_category(hints: str) -> str:
    if re.search(
        r"\bapply_patch\b|\b(?:cat|tee)\b[^\n]*(?:>>|>)|\b(?:sed|perl)\s+-i\b|\bgit\s+(?:mv|rm)\b",
        hints,
    ):
        return "file_changes"
    if "tool-results/mcp-" in hints or "tools.mcp__" in hints:
        return "external_service_data"
    if re.search(r"\b(kubectl|k9s?|psql|mysql|datadog|sentry|posthog)\b", hints):
        return "production_systems"
    if re.search(r"\.(?:pdf|docx?|odt|rtf|pages|md|txt)(?:\s|['\"]|$)", hints):
        return "documents"
    if "library/application support" in hints or re.search(r"\bsqlite3\b", hints):
        return "local_system_data"
    if re.search(r"\b(curl|wget)\b", hints) or re.search(r"https?://", hints):
        return "web_and_external"
    if re.search(r"\b(docker|podman)\s+logs\b|\bjournalctl\b", hints) or re.search(
        r"(?:^|[\s/])[^\s]+\.logs?(?:\s|$)", hints
    ):
        return "local_logs"
    if re.search(
        r"\b(pytest|unittest|ruff|mypy|eslint|jest|vitest|cargo\s+test|go\s+test|npm\s+(?:run\s+)?(?:test|build)|pnpm\s+(?:test|build)|yarn\s+(?:test|build))\b",
        hints,
    ):
        return "tests_and_builds"
    if re.search(
        r"(?:^|[;&|]\s*|\b)(?:rg|grep|find|fd|cat|sed|head|tail|jq|ls|tree|wc|git\s+(?:show|diff|status|log|blame))\b",
        hints,
    ):
        return "repository_and_files"
    return "local_system_data"


def _mcp_category(normalized_name: str, hints: str) -> str:
    identity = f"{normalized_name.replace('_', ' ')} {hints}"
    if re.search(
        r"\b(db|database|postgres|mysql|kubectl|datadog|sentry|posthog|production|prod|logs?)\b",
        identity,
    ):
        return "production_systems"
    if re.search(r"\b(github|gitlab|repository|repo)\b", identity):
        return "repository_and_files"
    return "external_service_data"


def _category(tool_name: str, label: str, arguments: object = None) -> str:
    normalized_name = (
        (_nested_tool_name(arguments) or tool_name).lower().replace("-", "_")
    )
    name_parts = set(filter(None, re.split(r"[^a-z0-9]+", normalized_name)))
    hints = _argument_hints(arguments).lower()
    suffix = Path(label.lower()).suffix
    if suffix == ".pdf" or "application/pdf" in hints:
        return "documents"
    if suffix in {".doc", ".docx", ".md", ".odt", ".pages", ".rtf", ".txt"}:
        return "documents"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}:
        return "images"
    if normalized_name in {"image_gen__imagegen", "imagegen"}:
        return "images"
    if normalized_name == "write_stdin":
        return "local_system_data"
    if normalized_name == "wait":
        return "subagent_handoffs"
    if normalized_name == "js":
        return (
            "local_system_data"
            if "getapp(" in hints.replace(" ", "")
            else "web_and_external"
        )
    if normalized_name in {"queued_command", "askuserquestion"}:
        return "prompts"
    if normalized_name in {
        "agent_listing_delta",
        "auto_mode",
        "bash_output_audience_note",
        "command_permissions",
        "credential_org",
        "date",
        "date_change",
        "model",
        "prompt_snapshot",
        "remote_session_change",
        "session_context",
    }:
        return "skills_and_instructions"
    if normalized_name == "edited_text_file":
        return "file_changes"
    if normalized_name == "file":
        return "repository_and_files"
    if normalized_name == "artifact":
        return "file_changes"
    if normalized_name == "compact_file_reference":
        return "previous_compact"
    if normalized_name in {"list_mcp_resources", "list_mcp_resource_templates"}:
        return "skills_and_instructions"
    if normalized_name == "read_mcp_resource":
        return _mcp_category(normalized_name, hints)
    if normalized_name in {"list_agents", "wait_agent"}:
        return "subagent_handoffs"
    if (
        {"web", "browser"} & name_parts
        or normalized_name
        in {"web_search", "web_fetch", "websearch", "webfetch", "open_url"}
        or hints.startswith(("http://", "https://"))
    ):
        return "web_and_external"
    if normalized_name.startswith("mcp__"):
        return _mcp_category(normalized_name, hints)
    if normalized_name in {
        "agent",
        "spawn_agent",
        "followup_task",
        "send_message",
        "task",
    } or (name_parts & {"subagent", "handoff"}):
        return "subagent_handoffs"
    if normalized_name == "toolsearch" or name_parts & {
        "skill",
        "skills",
        "plugin",
        "plugins",
        "instruction",
    }:
        return "skills_and_instructions"
    if normalized_name in {
        "apply_patch",
        "create_file",
        "edit",
        "edit_file",
        "write",
        "write_file",
    }:
        return "file_changes"
    if normalized_name in {"bash", "exec", "exec_command", "shell"}:
        return _shell_category(hints)
    if name_parts & {
        "read",
        "grep",
        "glob",
        "search",
        "view",
    } or normalized_name in {"read_file", "view_image"}:
        return "repository_and_files"
    return "other_tool_output"


def _epoch(
    index: int, iteration: int, timestamp: float | None = None
) -> dict[str, object]:
    return {
        "index": index,
        "iteration_start": iteration,
        "iteration_end": None,
        "started_at": _iso(timestamp) if timestamp is not None else None,
        "ended_at": None,
        "observed_context_tokens": None,
        "checkpoint_count": 0,
        "categories": {},
        "events": [],
    }


def _new_state(provider: str, session_id: str, path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "version": STATE_VERSION,
        "provider": provider,
        "session_id": session_id,
        "source_path": str(path),
        "source_device": stat.st_dev,
        "source_inode": stat.st_ino,
        "cursor": 0,
        "iteration": 0,
        "turn_complete": True,
        "current_epoch": 0,
        "active_model": "unknown",
        "pending_calls": {},
        "pending_sources": [],
        "deferred_sources": [],
        "previous_checkpoint": None,
        "epochs": [_epoch(0, 0)],
    }


def _load_state(provider: str, session_id: str, path: Path) -> dict[str, object]:
    destination = context_map_path(provider, session_id)
    try:
        parsed = json.loads(destination.read_text())
    except (OSError, json.JSONDecodeError):
        return _new_state(provider, session_id, path)
    if not isinstance(parsed, dict):
        return _new_state(provider, session_id, path)
    try:
        stat = path.stat()
    except OSError:
        return parsed
    same_source = (
        parsed.get("source_path") == str(path)
        and parsed.get("source_device") == stat.st_dev
        and parsed.get("source_inode") == stat.st_ino
    )
    if (
        parsed.get("version") != STATE_VERSION
        or not isinstance(parsed.get("epochs"), list)
        or not parsed["epochs"]
    ):
        rebuilt = _new_state(provider, session_id, path)
        analysis = parsed.get("analysis")
        if same_source and isinstance(analysis, dict):
            rebuilt["analysis"] = analysis
        return rebuilt
    if (
        not same_source
        or not isinstance(parsed.get("cursor"), int)
        or int(parsed["cursor"]) > stat.st_size
    ):
        return _new_state(provider, session_id, path)
    return parsed


def _current_epoch(state: dict[str, object]) -> dict[str, object]:
    epochs = state.get("epochs")
    assert isinstance(epochs, list) and epochs and isinstance(epochs[-1], dict)
    return cast(dict[str, object], epochs[-1])


def _pending_calls(state: dict[str, object]) -> dict[str, object]:
    value = state.get("pending_calls")
    if not isinstance(value, dict):
        value = {}
        state["pending_calls"] = value
    return value


def _pending_sources(state: dict[str, object]) -> list[dict[str, object]]:
    value = state.get("pending_sources")
    if not isinstance(value, list):
        value = []
        state["pending_sources"] = value
    return [item for item in value if isinstance(item, dict)]


def _source(
    *,
    source_id: str,
    timestamp: float | None,
    category: str,
    tool: str,
    label: str,
    weight_tokens: int,
    tokenizer: str,
    source_start: int,
    source_end: int,
) -> dict[str, object]:
    return {
        "id": source_id,
        "observed_at": _iso(timestamp) if timestamp is not None else None,
        "category": category,
        "tool": tool or "unknown",
        "label": label[:120] or "Context addition",
        "weight_tokens": max(1, weight_tokens),
        "tokenizer": tokenizer,
        "source_start": source_start,
        "source_end": source_end,
    }


def _append_pending(state: dict[str, object], source: dict[str, object]) -> None:
    pending = state.get("pending_sources")
    if not isinstance(pending, list):
        pending = []
        state["pending_sources"] = pending
    pending.append(source)


def _deferred_sources(state: dict[str, object]) -> list[dict[str, object]]:
    value = state.get("deferred_sources")
    if not isinstance(value, list):
        value = []
        state["deferred_sources"] = value
    return [item for item in value if isinstance(item, dict)]


def _append_deferred(state: dict[str, object], source: dict[str, object]) -> None:
    deferred = state.get("deferred_sources")
    if not isinstance(deferred, list):
        deferred = []
        state["deferred_sources"] = deferred
    deferred.append(source)


def _event_ids(epoch: dict[str, object]) -> set[str]:
    events = epoch.get("events")
    if not isinstance(events, list):
        return set()
    return {
        str(event.get("id"))
        for event in events
        if isinstance(event, dict) and isinstance(event.get("id"), str)
    }


def _append_event(
    state: dict[str, object],
    source: dict[str, object],
    tokens: int,
    model: str,
    checkpoint_id: str,
    confidence: str,
) -> None:
    epoch = _current_epoch(state)
    if str(source.get("id")) in _event_ids(epoch):
        return
    events = epoch.get("events")
    assert isinstance(events, list)
    event = dict(source)
    event.pop("weight_tokens", None)
    event.update(
        {
            "iteration": _integer(state.get("iteration")),
            "estimated_tokens": max(0, tokens),
            "tokenizer_estimate_tokens": _integer(source.get("weight_tokens"), 1),
            "model": model or "unknown",
            "checkpoint_id": checkpoint_id,
            "confidence": confidence,
        }
    )
    events.append(event)


def _allocate(total: int, weights: list[int]) -> list[int]:
    if not weights:
        return []
    denominator = sum(max(1, weight) for weight in weights)
    exact = [total * max(1, weight) / denominator for weight in weights]
    result = [math.floor(value) for value in exact]
    remainder = total - sum(result)
    order = sorted(
        range(len(weights)),
        key=lambda index: exact[index] - result[index],
        reverse=True,
    )
    for index in order[:remainder]:
        result[index] += 1
    return result


def _commit_window(
    state: dict[str, object],
    sources: list[dict[str, object]],
    *,
    total_tokens: int | None,
    model: str,
    checkpoint_id: str,
    initial: bool = False,
    confidence: str = "measured_window",
    residual_category: str = "provider_internal",
    residual_label: str = "Unmatched context",
) -> None:
    if total_tokens is None:
        for source in sources:
            _append_event(
                state,
                source,
                _integer(source.get("weight_tokens"), 1),
                model,
                checkpoint_id,
                confidence,
            )
        return
    total_tokens = max(0, total_tokens)
    weights = [_integer(source.get("weight_tokens"), 1) for source in sources]
    known_total = total_tokens
    if initial and weights:
        known_total = min(total_tokens, sum(weights))
    elif (
        weights
        and total_tokens > sum(weights) * 1.5
        and not any(
            source.get("category") in {"documents", "images"} for source in sources
        )
    ):
        known_total = min(total_tokens, round(sum(weights) * 1.1))
    for source, tokens in zip(sources, _allocate(known_total, weights)):
        _append_event(state, source, tokens, model, checkpoint_id, confidence)
    residual = total_tokens - known_total
    if residual > 0 or not sources:
        loaded_tools = any(
            str(source.get("tool") or "").lower() == "toolsearch" for source in sources
        )
        if loaded_tools and residual_category == "provider_internal":
            residual_category = "skills_and_instructions"
            residual_label = "Dynamically loaded tool definitions"
        category = "starting_context" if initial else residual_category
        unknown = _source(
            source_id=f"{checkpoint_id}:internal",
            timestamp=None,
            category=category,
            tool="provider",
            label=(
                "Context present when monitoring started" if initial else residual_label
            ),
            weight_tokens=max(1, residual or total_tokens),
            tokenizer="provider_checkpoint",
            source_start=0,
            source_end=0,
        )
        _append_event(
            state,
            unknown,
            residual or total_tokens,
            model,
            checkpoint_id,
            "measured_residual",
        )


def _assistant_output_source(previous: dict[str, object]) -> dict[str, object] | None:
    output_tokens = _integer(previous.get("output_tokens"))
    if output_tokens <= 0:
        return None
    raw_timestamp = previous.get("timestamp")
    return _source(
        source_id=f"{previous.get('id', 'unknown')}:assistant-output",
        timestamp=(
            float(raw_timestamp) if isinstance(raw_timestamp, (int, float)) else None
        ),
        category="assistant_output",
        tool="assistant",
        label="Agent responses and reasoning",
        weight_tokens=output_tokens,
        tokenizer="provider_output_count",
        source_start=_integer(previous.get("source_start")),
        source_end=_integer(previous.get("source_end")),
    ) | _output_ranges(previous)


def _output_ranges(previous: dict[str, object]) -> dict[str, object]:
    """Byte ranges of the reply records, so analysis reads the reply, not the usage record."""
    ranges = previous.get("output_ranges")
    if not isinstance(ranges, list) or not ranges:
        return {}
    return {"excerpt_ranges": ranges[-MAX_OUTPUT_EXCERPT_RANGES:]}


def _add_output_range(checkpoint: object, record: Record) -> None:
    if not isinstance(checkpoint, dict):
        return
    ranges = checkpoint.setdefault("output_ranges", [])
    if isinstance(ranges, list):
        ranges.append([record["start"], record["end"]])
        del ranges[:-MAX_OUTPUT_EXCERPT_RANGES]


def _file_write_argument_source(
    metadata: dict[str, object],
    record: Record,
    timestamp: float | None,
    call_id: str,
) -> dict[str, object] | None:
    """Keep a derived file-write weight without persisting the patch text."""
    if metadata.get("category") != "file_changes":
        return None
    tokens = _integer(metadata.get("argument_tokens"))
    if tokens <= 0:
        return None
    return _source(
        source_id=f"{record['digest']}:{record['start']}:{call_id}:arguments",
        timestamp=timestamp,
        category="file_changes",
        tool=str(metadata.get("name") or "file write"),
        label=str(metadata.get("label") or "File write"),
        weight_tokens=tokens,
        tokenizer=str(metadata.get("argument_tokenizer") or "unknown"),
        source_start=record["start"],
        source_end=record["end"],
    )


def _record_output_source(
    state: dict[str, object], source: dict[str, object] | None
) -> None:
    if source is None:
        return
    checkpoint = state.get("previous_checkpoint")
    if not isinstance(checkpoint, dict):
        return
    output_sources = checkpoint.setdefault("output_sources", [])
    if not isinstance(output_sources, list):
        checkpoint["output_sources"] = output_sources = []
    source_id = str(source.get("id") or "")
    call_suffix = source_id.rsplit(":", 2)[-2:]
    if any(
        isinstance(existing, dict)
        and (
            existing.get("id") == source_id
            or str(existing.get("id") or "").rsplit(":", 2)[-2:] == call_suffix
        )
        for existing in output_sources
    ):
        return
    output_sources.append(source)


def _assistant_output_sources(previous: dict[str, object]) -> list[dict[str, object]]:
    """Split provider-recorded output between file writes and ordinary output."""
    total = _integer(previous.get("output_tokens"))
    if total <= 0:
        return []
    raw_sources = previous.get("output_sources")
    sources = (
        [source for source in raw_sources if isinstance(source, dict)]
        if isinstance(raw_sources, list)
        else []
    )
    weights = sum(_integer(source.get("weight_tokens"), 1) for source in sources)
    generic = _assistant_output_source(previous)
    if generic is not None and total > weights:
        generic["weight_tokens"] = total - weights
        sources.append(generic)
    if not sources and generic is not None:
        sources.append(generic)
    return sources


def _append_assistant_output(
    state: dict[str, object],
    previous: dict[str, object],
    total_tokens: int,
    checkpoint_id: str,
    confidence: str,
) -> None:
    sources = _assistant_output_sources(previous)
    if not sources or total_tokens <= 0:
        return
    weights = [_integer(source.get("weight_tokens"), 1) for source in sources]
    for source, tokens in zip(sources, _allocate(total_tokens, weights)):
        _append_event(
            state,
            source,
            tokens,
            str(previous.get("model") or "unknown"),
            checkpoint_id,
            confidence,
        )


def _commit_checkpoint_window(
    state: dict[str, object],
    previous: dict[str, object],
    sources: list[dict[str, object]],
    *,
    input_tokens: int,
    model: str,
    checkpoint_id: str,
    source_model: str | None = None,
    residual_category: str = "provider_internal",
    residual_label: str = "Unmatched context",
) -> None:
    previous_model = str(previous.get("model") or "unknown")
    assistant_sources = _assistant_output_sources(previous)
    growth = (
        input_tokens - _integer(previous.get("input_tokens"))
        if previous_model == model
        else None
    )
    if growth is not None and growth < 0:
        growth = None
    if growth is None:
        _append_assistant_output(
            state,
            previous,
            _integer(previous.get("output_tokens")),
            checkpoint_id,
            "provider_reported",
        )
        _commit_window(
            state,
            sources,
            total_tokens=None,
            model=source_model or model,
            checkpoint_id=checkpoint_id,
            confidence="estimated_model_switch",
        )
        return
    assistant_tokens = _integer(previous.get("output_tokens"))
    if assistant_sources and growth >= assistant_tokens:
        _append_assistant_output(
            state,
            previous,
            assistant_tokens,
            checkpoint_id,
            "provider_reported",
        )
        _commit_window(
            state,
            sources,
            total_tokens=growth - assistant_tokens,
            model=previous_model,
            checkpoint_id=checkpoint_id,
            residual_category=residual_category,
            residual_label=residual_label,
        )
        return
    combined = assistant_sources + sources
    _commit_window(
        state,
        combined,
        total_tokens=growth,
        model=previous_model,
        checkpoint_id=checkpoint_id,
        residual_category=residual_category,
        residual_label=residual_label,
    )


def _refresh_categories(epoch: dict[str, object]) -> None:
    events = epoch.get("events")
    categories: dict[str, int] = {}
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            category = str(event.get("category") or "other_tool_output")
            categories[category] = categories.get(category, 0) + _integer(
                event.get("estimated_tokens")
            )
    epoch["categories"] = categories


def _reconcile_epoch(
    epoch: dict[str, object], observed_tokens: int, checkpoint_id: str
) -> None:
    events = epoch.get("events")
    if not isinstance(events, list):
        return
    typed_events = [event for event in events if isinstance(event, dict)]
    mapped = sum(_integer(event.get("estimated_tokens")) for event in typed_events)
    if mapped > observed_tokens and mapped > 0:
        projected = _allocate(
            observed_tokens,
            [_integer(event.get("estimated_tokens")) for event in typed_events],
        )
        for event, tokens in zip(typed_events, projected):
            event["estimated_tokens"] = tokens
    elif mapped < observed_tokens:
        events.append(
            {
                "id": f"{checkpoint_id}:reconciliation",
                "observed_at": None,
                "category": "provider_internal",
                "tool": "provider",
                "label": "Provider-managed context",
                "tokenizer": "provider_checkpoint",
                "source_start": 0,
                "source_end": 0,
                "iteration": epoch.get("iteration_end", 0),
                "estimated_tokens": observed_tokens - mapped,
                "model": "unknown",
                "checkpoint_id": checkpoint_id,
                "confidence": "measured_residual",
            }
        )
    epoch["observed_context_tokens"] = observed_tokens
    _refresh_categories(epoch)


def _mapped_tokens(epoch: dict[str, object]) -> int:
    _refresh_categories(epoch)
    categories = epoch.get("categories")
    return (
        sum(_integer(value) for value in categories.values())
        if isinstance(categories, dict)
        else 0
    )


def _start_epoch(
    state: dict[str, object],
    timestamp: float | None,
    pre_tokens: int | None,
    post_tokens: int | None,
    record: Record,
    post_tokens_measured: bool = True,
) -> None:
    previous = _current_epoch(state)
    previous["ended_at"] = _iso(timestamp) if timestamp is not None else None
    previous["iteration_end"] = _integer(state.get("iteration"))
    pending = _pending_sources(state) + _deferred_sources(state)
    checkpoint = state.get("previous_checkpoint")
    if pre_tokens is not None:
        if isinstance(checkpoint, dict):
            _commit_checkpoint_window(
                state,
                checkpoint,
                pending,
                input_tokens=pre_tokens,
                model=str(state.get("active_model") or "unknown"),
                checkpoint_id=record["digest"][:16],
                source_model=str(state.get("active_model") or "unknown"),
            )
        else:
            _commit_window(
                state,
                pending,
                total_tokens=max(0, pre_tokens - _mapped_tokens(previous)),
                model=str(state.get("active_model") or "unknown"),
                checkpoint_id=record["digest"][:16],
            )
        _reconcile_epoch(previous, pre_tokens, record["digest"][:16])
    else:
        if isinstance(checkpoint, dict):
            _append_assistant_output(
                state,
                checkpoint,
                _integer(checkpoint.get("output_tokens")),
                record["digest"][:16],
                "provider_reported",
            )
        _commit_window(
            state,
            pending,
            total_tokens=None,
            model=str(state.get("active_model") or "unknown"),
            checkpoint_id=record["digest"][:16],
            confidence="estimated_before_compact",
        )
        _refresh_categories(previous)
        previous["observed_context_tokens"] = _mapped_tokens(previous)
        previous["context_total_confidence"] = "estimated_before_compact"
    index = _integer(state.get("current_epoch")) + 1
    state["current_epoch"] = index
    epochs = state.get("epochs")
    assert isinstance(epochs, list)
    epochs.append(_epoch(index, _integer(state.get("iteration")), timestamp))
    state["pending_calls"] = {}
    state["pending_sources"] = []
    state["deferred_sources"] = []
    state["previous_checkpoint"] = None
    if post_tokens is not None and post_tokens > 0:
        source = _source(
            source_id=f"{record['digest']}:{record['start']}:compact",
            timestamp=timestamp,
            category="previous_compact",
            tool="compact",
            label="Previous compact",
            weight_tokens=post_tokens,
            tokenizer="provider_compact",
            source_start=record["start"],
            source_end=record["end"],
        )
        if post_tokens_measured:
            _append_event(
                state,
                source,
                post_tokens,
                str(state.get("active_model") or "unknown"),
                record["digest"][:16],
                "measured_compact",
            )
            _current_epoch(state)["observed_context_tokens"] = post_tokens
        else:
            _append_pending(state, source)


def _image_dimensions(raw: bytes) -> tuple[int, int] | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if raw[:3] == b"GIF" and len(raw) >= 10:
        return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")
    if not raw.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 9 < len(raw):
        if raw[offset] != 0xFF:
            offset += 1
            continue
        marker = raw[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(raw):
            break
        length = int.from_bytes(raw[offset : offset + 2], "big")
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            return (
                int.from_bytes(raw[offset + 5 : offset + 7], "big"),
                int.from_bytes(raw[offset + 3 : offset + 5], "big"),
            )
        offset += max(2, length)
    return None


def _decoded_image_dimensions(block: dict[str, object]) -> tuple[int, int] | None:
    source = block.get("source") or block.get("image_url") or block.get("data")
    if isinstance(source, dict):
        source = source.get("data") or source.get("url")
    if not isinstance(source, str):
        return None
    encoded = (
        source.split(",", 1)[1]
        if source.startswith("data:") and "," in source
        else source
    )
    try:
        return _image_dimensions(base64.b64decode(encoded, validate=False))
    except (ValueError, TypeError):
        return None


def _result_sources(
    tokenizer: ContextTokenizer,
    provider: str,
    model: str,
    content: object,
    metadata: dict[str, object],
    record: Record,
    timestamp: float | None,
    call_id: str,
) -> list[dict[str, object]]:
    tool = str(metadata.get("name") or "unknown")
    label = str(metadata.get("label") or tool)
    category = str(metadata.get("category") or "other_tool_output")
    blocks = content if isinstance(content, list) else [content]
    result: list[dict[str, object]] = []
    # Offsets of the call itself, so analysis can see which file or command produced this.
    call_range = metadata.get("call_range")
    extra: dict[str, object] = (
        {"call_range": call_range} if isinstance(call_range, list) else {}
    )
    text_blocks: list[object] = []
    for index, block in enumerate(blocks):
        kind = block.get("type") if isinstance(block, dict) else None
        if kind in {"image", "input_image"}:
            dimensions = _decoded_image_dimensions(block)
            if dimensions is None:
                weight = 1
                name = "image-checkpoint"
            elif provider == "claude":
                width, height = dimensions
                weight = math.ceil(width / 28) * math.ceil(height / 28)
                name = "claude-image-grid"
            else:
                width, height = dimensions
                weight = math.ceil(math.ceil(width / 32) * math.ceil(height / 32) * 1.2)
                name = "codex-image-patches"
            result.append(
                _source(
                    source_id=(
                        f"{record['digest']}:{record['start']}:{call_id}:image:{index}"
                    ),
                    timestamp=timestamp,
                    category="images",
                    tool=tool,
                    label=label,
                    weight_tokens=weight,
                    tokenizer=name,
                    source_start=record["start"],
                    source_end=record["end"],
                )
                | extra
            )
        elif kind in {"document", "input_document"}:
            result.append(
                _source(
                    source_id=(
                        f"{record['digest']}:{record['start']}:{call_id}:document:{index}"
                    ),
                    timestamp=timestamp,
                    category="documents",
                    tool=tool,
                    label=label,
                    weight_tokens=1,
                    tokenizer="provider-document-checkpoint",
                    source_start=record["start"],
                    source_end=record["end"],
                )
                | extra
            )
        else:
            text_blocks.append(block)
    if text_blocks:
        tokens, name = tokenizer.count(provider, model, text_blocks)
        family = name.removeprefix("ctok-")
        frame = (
            CLAUDE_RESULT_FRAME_TOKENS.get(family, 23)
            if provider == "claude"
            else CODEX_RESULT_FRAME_TOKENS
        )
        result.append(
            _source(
                source_id=f"{record['digest']}:{record['start']}:{call_id}:text",
                timestamp=timestamp,
                category=category,
                tool=tool,
                label=label,
                weight_tokens=tokens + frame,
                tokenizer=name,
                source_start=record["start"],
                source_end=record["end"],
            )
            | extra
        )
    return result


def _call_metadata(
    tokenizer: ContextTokenizer,
    provider: str,
    model: str,
    tool_name: str,
    arguments: object,
) -> dict[str, object]:
    effective_name = _nested_tool_name(arguments) or tool_name or "unknown"
    label = _argument_label(arguments) or effective_name or "Tool output"
    tokens, name = tokenizer.count(provider, model, arguments)
    return {
        "name": effective_name,
        "label": label,
        "category": _category(effective_name, label, arguments),
        "argument_tokens": tokens,
        "argument_tokenizer": name,
    }


def _claude_input_total(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    values = [
        usage.get("input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0),
        usage.get("cache_read_input_tokens", 0),
    ]
    if not all(
        isinstance(value, int) and not isinstance(value, bool) for value in values
    ):
        return None
    return sum(cast(list[int], values))


def _prompt_sources(
    tokenizer: ContextTokenizer,
    provider: str,
    model: str,
    content: object,
    record: Record,
    timestamp: float | None,
) -> list[dict[str, object]]:
    sources = _result_sources(
        tokenizer,
        provider,
        model,
        content,
        {"name": "prompt", "label": "User prompt", "category": "prompts"},
        record,
        timestamp,
        "prompt",
    )
    frame = (
        CLAUDE_RESULT_FRAME_TOKENS.get(
            next(
                (
                    str(source.get("tokenizer")).removeprefix("ctok-")
                    for source in sources
                    if source.get("category") == "prompts"
                ),
                "5.0",
            ),
            23,
        )
        if provider == "claude"
        else CODEX_RESULT_FRAME_TOKENS
    )
    for source in sources:
        if source.get("category") == "prompts":
            source["weight_tokens"] = max(
                1, _integer(source.get("weight_tokens")) - frame
            )
            source["id"] = f"{record['digest']}:{record['start']}:prompt:text"
    return sources


def _codex_user_sources(
    tokenizer: ContextTokenizer,
    model: str,
    content: object,
    record: Record,
    timestamp: float | None,
) -> list[dict[str, object]]:
    sources = _prompt_sources(tokenizer, "codex", model, content, record, timestamp)
    text = ""
    if isinstance(content, str):
        text = content.lstrip()
    elif isinstance(content, list):
        for block in content:
            candidate = block.get("text") if isinstance(block, dict) else None
            if isinstance(candidate, str):
                text = candidate.lstrip()
                break
    markers = (
        "# AGENTS.md instructions",
        "<apps_instructions>",
        "<collaboration_mode>",
        "<environment_context>",
        "<permissions instructions>",
        "<plugins_instructions>",
        "<skills_instructions>",
    )
    if text.startswith(markers):
        for source in sources:
            if source.get("category") == "prompts":
                source["category"] = "skills_and_instructions"
                source["tool"] = "instructions"
                source["label"] = "Session instructions"
    return sources


def _claude_attachment_content(attachment: dict[str, object]) -> object:
    for key in (
        "content",
        "text",
        "addedLines",
        "addedBlocks",
        "changes",
        "snapshot",
        "stdout",
    ):
        value = attachment.get(key)
        if value not in (None, "", [], {}):
            return value
    return attachment


def _process_claude(
    state: dict[str, object], record: Record, tokenizer: ContextTokenizer
) -> None:
    value = record["value"]
    if value is None:
        _append_pending(
            state,
            _source(
                source_id=f"{record['digest']}:{record['start']}:unparsed",
                timestamp=None,
                category="other_tool_output",
                tool="unparsed_record",
                label="Unparsed transcript record",
                weight_tokens=math.ceil(record["size"] / 4),
                tokenizer="byte-size-fallback",
                source_start=record["start"],
                source_end=record["end"],
            ),
        )
        return
    timestamp = parse_timestamp(value.get("timestamp"))
    if value.get("type") == "system" and value.get("subtype") == "compact_boundary":
        metadata = value.get("compactMetadata")
        _start_epoch(
            state,
            timestamp,
            _number(metadata.get("preTokens")) if isinstance(metadata, dict) else None,
            _number(metadata.get("postTokens")) if isinstance(metadata, dict) else None,
            record,
        )
        return
    if value.get("type") == "attachment":
        attachment = value.get("attachment")
        if not isinstance(attachment, dict):
            return
        attachment_type = str(attachment.get("type") or "attachment")
        normalized = attachment_type.lower()
        category = _category(
            attachment_type,
            _argument_label(attachment) or attachment_type,
            attachment,
        )
        if category == "other_tool_output" and (
            normalized in SESSION_SETTING_ATTACHMENTS
            or any(
                marker in normalized
                for marker in (
                    "environment",
                    "hook",
                    "instruction",
                    "mcp",
                    "reminder",
                    "skill",
                    "tool",
                )
            )
        ):
            category = "skills_and_instructions"
        for source in _result_sources(
            tokenizer,
            "claude",
            str(state.get("active_model") or "unknown"),
            _claude_attachment_content(attachment),
            {
                "name": attachment_type,
                "label": attachment_type.replace("_", " ").title(),
                "category": category,
            },
            record,
            timestamp,
            "attachment",
        ):
            _append_pending(state, source)
        return
    message = value.get("message")
    if not isinstance(message, dict):
        return
    role = message.get("role")
    model = str(message.get("model") or state.get("active_model") or "unknown")
    if role == "assistant":
        if model == "<synthetic>":
            return
        state["active_model"] = model
        usage = message.get("usage")
        input_tokens = _claude_input_total(usage)
        output_tokens = (
            _integer(usage.get("output_tokens")) if isinstance(usage, dict) else 0
        )
        diagnostics = message.get("diagnostics")
        cache_miss = (
            diagnostics.get("cache_miss_reason")
            if isinstance(diagnostics, dict)
            else None
        )
        tools_changed = (
            isinstance(cache_miss, dict) and cache_miss.get("type") == "tools_changed"
        )
        message_id = message.get("id")
        checkpoint_id = (
            message_id if isinstance(message_id, str) else record["digest"][:16]
        )
        is_new_checkpoint = state.get("last_claude_checkpoint_id") != checkpoint_id
        if input_tokens is not None and is_new_checkpoint:
            previous = state.get("previous_checkpoint")
            sources = _pending_sources(state)
            initial = not isinstance(previous, dict)
            if isinstance(previous, dict):
                _commit_checkpoint_window(
                    state,
                    previous,
                    sources,
                    input_tokens=input_tokens,
                    model=model,
                    checkpoint_id=checkpoint_id,
                    source_model=model,
                    residual_category=(
                        "skills_and_instructions"
                        if tools_changed
                        else "provider_internal"
                    ),
                    residual_label=(
                        "Claude tool definitions"
                        if tools_changed
                        else "Unmatched context"
                    ),
                )
            else:
                _commit_window(
                    state,
                    sources,
                    total_tokens=max(
                        0, input_tokens - _mapped_tokens(_current_epoch(state))
                    ),
                    model=model,
                    checkpoint_id=checkpoint_id,
                    initial=initial,
                    confidence="estimated_initial",
                )
            state["pending_sources"] = []
            state["previous_checkpoint"] = {
                "id": checkpoint_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model": model,
                "timestamp": timestamp,
                "source_start": record["start"],
                "source_end": record["end"],
            }
            epoch = _current_epoch(state)
            epoch["checkpoint_count"] = _integer(epoch.get("checkpoint_count")) + 1
            _reconcile_epoch(epoch, input_tokens, checkpoint_id)
            epoch["context_total_confidence"] = "provider_reported"
            state["last_claude_checkpoint_id"] = checkpoint_id
        pending = _pending_calls(state)
        content = message.get("content")
        checkpoint = state.get("previous_checkpoint")
        if (
            isinstance(content, list)
            and isinstance(checkpoint, dict)
            and checkpoint.get("id") == checkpoint_id
            and any(
                isinstance(block, dict) and block.get("type") in {"text", "tool_use"}
                for block in content
            )
        ):
            # Claude splits one reply across records; the first is often thinking only.
            _add_output_range(checkpoint, record)
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                call_id = block.get("id")
                if isinstance(call_id, str):
                    metadata = _call_metadata(
                        tokenizer,
                        "claude",
                        model,
                        str(block.get("name") or "unknown"),
                        block.get("input"),
                    )
                    metadata["call_range"] = [record["start"], record["end"]]
                    pending[call_id] = metadata
                    _record_output_source(
                        state,
                        _file_write_argument_source(
                            metadata, record, timestamp, call_id
                        ),
                    )
        return
    if role == "user" and value.get("isMeta") is True:
        for source in _result_sources(
            tokenizer,
            "claude",
            str(state.get("active_model") or "unknown"),
            message.get("content"),
            {
                "name": "instructions",
                "label": "Injected skill or system context",
                "category": "skills_and_instructions",
            },
            record,
            timestamp,
            "meta",
        ):
            _append_pending(state, source)
        return
    if is_human_claude_prompt(value):
        state["iteration"] = _integer(state.get("iteration")) + 1
        for source in _prompt_sources(
            tokenizer,
            "claude",
            str(state.get("active_model") or "unknown"),
            message.get("content"),
            record,
            timestamp,
        ):
            _append_pending(state, source)
    content = message.get("content")
    if not isinstance(content, list):
        return
    pending = _pending_calls(state)
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        call_id = block.get("tool_use_id")
        metadata = pending.pop(call_id, {}) if isinstance(call_id, str) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        for source in _result_sources(
            tokenizer,
            "claude",
            str(state.get("active_model") or "unknown"),
            block.get("content"),
            metadata,
            record,
            timestamp,
            call_id if isinstance(call_id, str) else "unknown",
        ):
            _append_pending(state, source)


def _decode_arguments(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _codex_usage(payload: dict[str, object]) -> tuple[int, int] | None:
    info = payload.get("info")
    usage = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(usage, dict):
        return None
    input_tokens = _number(usage.get("input_tokens"))
    output_tokens = _number(usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens, output_tokens


def _codex_checkpoint_id(payload: dict[str, object]) -> str | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    last = info.get("last_token_usage")
    total = info.get("total_token_usage")
    if not isinstance(last, dict):
        return None
    identity = {
        "input": last.get("input_tokens"),
        "output": last.get("output_tokens"),
        "reasoning": last.get("reasoning_output_tokens"),
        "total": total.get("total_tokens") if isinstance(total, dict) else None,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _process_codex(
    state: dict[str, object], record: Record, tokenizer: ContextTokenizer
) -> None:
    value = record["value"]
    if value is None:
        _append_pending(
            state,
            _source(
                source_id=f"{record['digest']}:{record['start']}:unparsed",
                timestamp=None,
                category="other_tool_output",
                tool="unparsed_record",
                label="Unparsed transcript record",
                weight_tokens=math.ceil(record["size"] / 4),
                tokenizer="byte-size-fallback",
                source_start=record["start"],
                source_end=record["end"],
            ),
        )
        return
    timestamp = parse_timestamp(value.get("timestamp"))
    payload = value.get("payload")
    if value.get("type") == "compacted":
        replacement = (
            payload.get("replacement_history") if isinstance(payload, dict) else None
        )
        estimated, _ = tokenizer.count(
            "codex", str(state.get("active_model") or "unknown"), replacement
        )
        _start_epoch(
            state,
            timestamp,
            None,
            estimated or None,
            record,
            post_tokens_measured=False,
        )
        return
    if value.get("type") == "turn_context" and isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, str) and model:
            state["active_model"] = model
        return
    if not isinstance(payload, dict):
        return
    if value.get("type") == "event_msg" and payload.get("type") == "task_started":
        state["iteration"] = _integer(state.get("iteration")) + 1
        state["turn_complete"] = False
        return
    if value.get("type") == "event_msg" and payload.get("type") in {
        "task_complete",
        "turn_aborted",
    }:
        state["turn_complete"] = True
        return
    if value.get("type") == "event_msg" and payload.get("type") == "user_message":
        content = payload.get("message") or payload.get("text")
        for source in _codex_user_sources(
            tokenizer,
            str(state.get("active_model") or "unknown"),
            content,
            record,
            timestamp,
        ):
            _append_pending(state, source)
        return
    if value.get("type") == "event_msg" and payload.get("type") == "token_count":
        usage = _codex_usage(payload)
        if usage is None:
            return
        logical_checkpoint_id = _codex_checkpoint_id(payload)
        if (
            logical_checkpoint_id is not None
            and state.get("last_codex_checkpoint_id") == logical_checkpoint_id
        ):
            return
        input_tokens, output_tokens = usage
        if input_tokens == 0 and output_tokens == 0:
            state["last_codex_checkpoint_id"] = logical_checkpoint_id
            return
        previous = state.get("previous_checkpoint")
        current_sources = _pending_sources(state)
        if isinstance(previous, dict):
            active_model = str(state.get("active_model") or "unknown")
            _commit_checkpoint_window(
                state,
                previous,
                current_sources,
                input_tokens=input_tokens,
                model=active_model,
                checkpoint_id=str(previous.get("id") or record["digest"][:16]),
                source_model=active_model,
            )
        elif input_tokens > 0:
            remaining = max(0, input_tokens - _mapped_tokens(_current_epoch(state)))
            _commit_window(
                state,
                current_sources,
                total_tokens=remaining,
                model=str(state.get("active_model") or "unknown"),
                checkpoint_id=f"{record['digest'][:16]}:initial",
                initial=True,
                confidence="estimated_initial",
            )
            state["pending_sources"] = []
        replies = state.pop("codex_reply_ranges", None)
        state["previous_checkpoint"] = {
            "id": record["digest"][:16],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": str(state.get("active_model") or "unknown"),
            "timestamp": timestamp,
            "source_start": record["start"],
            "source_end": record["end"],
            # Codex logs a call's replies before the token count that reports them.
            "output_ranges": replies if isinstance(replies, list) else [],
        }
        state["pending_sources"] = _deferred_sources(state)
        state["deferred_sources"] = []
        epoch = _current_epoch(state)
        epoch["checkpoint_count"] = _integer(epoch.get("checkpoint_count")) + 1
        _reconcile_epoch(
            epoch,
            input_tokens,
            logical_checkpoint_id or record["digest"][:16],
        )
        epoch["context_total_confidence"] = "provider_reported"
        state["last_codex_checkpoint_id"] = logical_checkpoint_id
        return
    if value.get("type") != "response_item":
        return
    item_type = payload.get("type")
    if item_type == "message" and payload.get("role") == "assistant":
        replies = state.setdefault("codex_reply_ranges", [])
        if isinstance(replies, list):
            replies.append([record["start"], record["end"]])
            del replies[:-MAX_OUTPUT_EXCERPT_RANGES]
        return
    if item_type == "message" and payload.get("role") == "user":
        for source in _codex_user_sources(
            tokenizer,
            str(state.get("active_model") or "unknown"),
            payload.get("content"),
            record,
            timestamp,
        ):
            _append_pending(state, source)
        return
    if item_type == "message" and payload.get("role") in {"developer", "system"}:
        for source in _result_sources(
            tokenizer,
            "codex",
            str(state.get("active_model") or "unknown"),
            payload.get("content"),
            {
                "name": "instructions",
                "label": "Session instructions",
                "category": "skills_and_instructions",
            },
            record,
            timestamp,
            "instructions",
        ):
            _append_pending(state, source)
        return
    pending = _pending_calls(state)
    if item_type in {"custom_tool_call", "function_call", "local_shell_call"}:
        call_id = payload.get("call_id") or payload.get("id")
        if isinstance(call_id, str):
            metadata = _call_metadata(
                tokenizer,
                "codex",
                str(state.get("active_model") or "unknown"),
                str(payload.get("name") or item_type),
                _decode_arguments(
                    payload.get("input")
                    or payload.get("arguments")
                    or payload.get("action")
                ),
            )
            metadata["call_range"] = [record["start"], record["end"]]
            pending[call_id] = metadata
            _record_output_source(
                state,
                _file_write_argument_source(metadata, record, timestamp, call_id),
            )
    elif item_type in {
        "custom_tool_call_output",
        "function_call_output",
        "local_shell_call_output",
    }:
        call_id = payload.get("call_id") or payload.get("id")
        popped_metadata = pending.pop(call_id, {}) if isinstance(call_id, str) else {}
        call_metadata = popped_metadata if isinstance(popped_metadata, dict) else {}
        for source in _result_sources(
            tokenizer,
            "codex",
            str(state.get("active_model") or "unknown"),
            payload.get("output"),
            call_metadata,
            record,
            timestamp,
            call_id if isinstance(call_id, str) else "unknown",
        ):
            _append_deferred(state, source)


def _finalize_state(state: dict[str, object], observed_context: int | None) -> None:
    epoch = _current_epoch(state)
    _refresh_categories(epoch)
    epoch["session_context_tokens"] = observed_context


def _summary(state: dict[str, object]) -> dict[str, object]:
    epoch = _current_epoch(state)
    categories = epoch.get("categories")
    typed_categories = (
        cast(dict[str, object], categories) if isinstance(categories, dict) else {}
    )
    observed = _number(epoch.get("observed_context_tokens"))
    mapped = sum(_integer(value) for value in typed_categories.values())
    if observed is not None and mapped > observed and mapped > 0:
        keys = list(typed_categories)
        projected = _allocate(
            observed,
            [_integer(typed_categories[key]) for key in keys],
        )
        typed_categories = dict(zip(keys, projected))
        mapped = sum(projected)
    if observed is not None and mapped < observed:
        typed_categories = dict(typed_categories)
        typed_categories["provider_internal"] = _integer(
            typed_categories.get("provider_internal")
        ) + (observed - mapped)
        mapped = observed
    epochs = state.get("epochs")
    summary = {
        "state": "ready" if mapped > 0 else "measuring",
        "epoch": state.get("current_epoch", 0),
        "iteration": state.get("iteration", 0),
        "turn_complete": state.get("turn_complete"),
        "observed_context_tokens": observed,
        "categories": typed_categories,
        "epoch_count": len(epochs) if isinstance(epochs, list) else 0,
    }
    analysis = state.get("analysis")
    if isinstance(analysis, dict) and isinstance(analysis.get("summary"), dict):
        summary["analysis"] = analysis["summary"]
    return summary


class ContextMapCollector:
    """Map new transcript records during the collector's existing refresh cycle."""

    def __init__(self, tokenizer: ContextTokenizer | None = None) -> None:
        self._tokenizer = tokenizer or ContextTokenizer()
        self._states: dict[tuple[str, str, str], dict[str, object]] = {}

    @staticmethod
    def _source_is_current(state: dict[str, object], path: Path) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        return (
            state.get("source_path") == str(path)
            and state.get("source_device") == stat.st_dev
            and state.get("source_inode") == stat.st_ino
            and _integer(state.get("cursor")) <= stat.st_size
        )

    def refresh(
        self,
        snapshot: dict[str, object],
        live_state: IncrementalLiveState,
    ) -> list[dict[str, object]]:
        return self.refresh_sources(snapshot, _context_sources(live_state))

    def refresh_sources(
        self,
        snapshot: dict[str, object],
        sources: list[tuple[str, str, Path]],
    ) -> list[dict[str, object]]:
        sessions = snapshot.get("sessions")
        if not isinstance(sessions, list):
            return []
        by_identity = {
            (session.get("provider"), session.get("id")): session
            for session in sessions
            if isinstance(session, dict)
        }
        results: list[dict[str, object]] = []
        for provider, session_id, path in sources:
            session = by_identity.get((provider, session_id))
            if not isinstance(session, dict):
                continue
            key = (provider, session_id, str(path))
            state = self._states.get(key)
            if state is None or not self._source_is_current(state, path):
                state = _load_state(provider, session_id, path)
                self._states[key] = state
            changed = False
            for record in _record_stream(path, _integer(state.get("cursor"))):
                if provider == "claude":
                    _process_claude(state, record, self._tokenizer)
                else:
                    _process_codex(state, record, self._tokenizer)
                state["cursor"] = record["end"]
                changed = True
            summary = state.get("summary")
            summary_missing = not isinstance(summary, dict)
            summary_stale = isinstance(summary, dict) and _number(
                summary.get("observed_context_tokens")
            ) != _number(session.get("context_tokens"))
            if changed or summary_missing or summary_stale:
                _finalize_state(state, _number(session.get("context_tokens")))
                state["updated_at"] = snapshot.get("generated_at")
                summary = _summary(state)
                state["summary"] = summary
            session["context_map"] = summary
            destination = context_map_path(provider, session_id)
            if changed or summary_missing or summary_stale or not destination.exists():
                write_private_json_if_changed(destination, state)
            results.append(
                {
                    "provider": provider,
                    "session_id": session_id,
                    "source_path": str(path),
                    "source_device": state.get("source_device"),
                    "source_inode": state.get("source_inode"),
                    "cursor": state.get("cursor"),
                    "summary": summary,
                }
            )
        return results


def _context_sources(
    live_state: IncrementalLiveState,
) -> list[tuple[str, str, Path]]:
    sources: list[tuple[str, str, Path]] = []
    for path, claude_live in live_state.claude.items():
        if claude_live.session_id and path.stem == claude_live.session_id:
            sources.append(("claude", claude_live.session_id, path))
    for path, codex_live in live_state.codex.items():
        if codex_live.session_id and codex_live.parent is None:
            sources.append(("codex", codex_live.session_id, path))
    return sources


WorkerRunner = Callable[[dict[str, object]], list[dict[str, object]]]


def _run_worker(payload: dict[str, object]) -> list[dict[str, object]]:
    completed = subprocess.run(
        [sys.executable, "-m", "konvu_telemetry.context_map", "--worker"],
        input=json.dumps(payload, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    parsed = json.loads(completed.stdout)
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


class ContextMapScheduler:
    """Run costly transcript mapping only when active transcript bytes change."""

    def __init__(self, runner: WorkerRunner = _run_worker) -> None:
        self._runner = runner
        self._entries: dict[tuple[str, str, str], dict[str, object]] = {}

    @staticmethod
    def _cached_entry(
        provider: str, session_id: str, path: Path
    ) -> dict[str, object] | None:
        state = _load_state(provider, session_id, path)
        summary = state.get("summary")
        if not isinstance(summary, dict):
            return None
        return {
            "provider": provider,
            "session_id": session_id,
            "source_path": str(path),
            "source_device": state.get("source_device"),
            "source_inode": state.get("source_inode"),
            "cursor": state.get("cursor"),
            "summary": summary,
        }

    @staticmethod
    def _changed(
        entry: dict[str, object] | None,
        path: Path,
        observed_context: int | None,
    ) -> bool:
        if entry is None:
            return True
        try:
            stat = path.stat()
        except OSError:
            return False
        cursor = _integer(entry.get("cursor"))
        summary = entry.get("summary")
        return (
            entry.get("source_device") != stat.st_dev
            or entry.get("source_inode") != stat.st_ino
            or cursor != stat.st_size
            or not isinstance(summary, dict)
            or _number(summary.get("observed_context_tokens")) != observed_context
        )

    def refresh(
        self,
        snapshot: dict[str, object],
        live_state: IncrementalLiveState,
    ) -> None:
        sessions = snapshot.get("sessions")
        if not isinstance(sessions, list):
            return
        sources = _context_sources(live_state)
        active_keys = {
            (provider, session_id, str(path)) for provider, session_id, path in sources
        }
        self._entries = {
            key: entry for key, entry in self._entries.items() if key in active_keys
        }
        for provider, session_id, path in sources:
            key = (provider, session_id, str(path))
            if key not in self._entries:
                cached = self._cached_entry(provider, session_id, path)
                if cached is not None:
                    self._entries[key] = cached
        by_identity = {
            (session.get("provider"), session.get("id")): session
            for session in sessions
            if isinstance(session, dict)
        }
        changed = []
        for provider, session_id, path in sources:
            session = by_identity.get((provider, session_id))
            observed = (
                _number(session.get("context_tokens"))
                if isinstance(session, dict)
                else None
            )
            if self._changed(
                self._entries.get((provider, session_id, str(path))),
                path,
                observed,
            ):
                changed.append((provider, session_id, path))
        if changed:
            wanted = {(provider, session_id) for provider, session_id, _ in changed}
            payload: dict[str, object] = {
                "snapshot": {
                    "generated_at": snapshot.get("generated_at"),
                    "sessions": [
                        session
                        for session in sessions
                        if isinstance(session, dict)
                        and (session.get("provider"), session.get("id")) in wanted
                    ],
                },
                "sources": [
                    {
                        "provider": provider,
                        "session_id": session_id,
                        "path": str(path),
                    }
                    for provider, session_id, path in changed
                ],
            }
            try:
                results = self._runner(payload)
            except Exception as error:
                LOGGER.warning("Context map worker failed: %s", type(error).__name__)
                results = []
            for entry in results:
                result_provider = entry.get("provider")
                result_session_id = entry.get("session_id")
                source_path = entry.get("source_path")
                if (
                    isinstance(result_provider, str)
                    and isinstance(result_session_id, str)
                    and isinstance(source_path, str)
                ):
                    self._entries[(result_provider, result_session_id, source_path)] = (
                        entry
                    )
        for (provider, session_id, _), entry in self._entries.items():
            session = by_identity.get((provider, session_id))
            summary = entry.get("summary")
            if isinstance(session, dict) and isinstance(summary, dict):
                session["context_map"] = summary


def _worker_main() -> None:
    payload = json.load(sys.stdin)
    snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
    raw_sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(snapshot, dict) or not isinstance(raw_sources, list):
        raise ValueError("invalid context worker payload")
    sources: list[tuple[str, str, Path]] = []
    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        provider = source.get("provider")
        session_id = source.get("session_id")
        path = source.get("path")
        if (
            isinstance(provider, str)
            and provider in {"claude", "codex"}
            and isinstance(session_id, str)
            and isinstance(path, str)
        ):
            sources.append((provider, session_id, Path(path)))
    results = ContextMapCollector().refresh_sources(snapshot, sources)
    print(json.dumps(results, separators=(",", ":")))


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker_main()
