"""Provider-neutral local event export."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import UsageEvent
from .parsers import codex_events_in_file, events_in_file
from .pricing import event_cost, load_pricing
from .storage import (
    claude_roots,
    codex_roots,
    normalized_events_path,
    transcript_files,
    write_private_json,
)


def normalized_event(
    event: UsageEvent, prices: dict[str, dict[str, float]]
) -> dict[str, object]:
    """Emit one provider-neutral model call for the local event store."""
    return {
        "provider": event.provider,
        "session_id": event.session_id,
        "event_id": event.message_id,
        "timestamp": datetime.fromtimestamp(event.timestamp, timezone.utc).isoformat(),
        "model": event.model,
        "tokens": {
            "input": event.usage.input_tokens,
            "output": event.usage.output_tokens,
            "reasoning_output": event.usage.reasoning_output_tokens,
            "cache_write": event.usage.cache_write_tokens,
            "cache_write_one_hour": event.usage.cache_write_one_hour_tokens,
            "cache_read": event.usage.cache_read_tokens,
            "web_search_requests": event.usage.web_search_requests,
        },
        "context_window_tokens": event.context_window_tokens,
        "estimated_cost_usd": event_cost(event, prices),
        "tool_calls": event.tool_calls,
        "is_subagent": event.is_subagent,
        "agent_id": event.agent_id,
        "reasoning_effort": event.effort,
        "speed": event.usage.speed,
    }


def write_normalized_events() -> None:
    """Create one local, provider-neutral event file from Claude and Codex logs."""
    prices = load_pricing()
    events: list[dict[str, object]] = []
    seen_claude_messages: set[str] = set()
    for root in claude_roots():
        if not root.is_dir():
            continue
        for transcript in transcript_files(root):
            for event in events_in_file(transcript):
                if event.message_id and event.message_id in seen_claude_messages:
                    continue
                if event.message_id:
                    seen_claude_messages.add(event.message_id)
                events.append(normalized_event(event, prices))
    for root in codex_roots():
        if not root.is_dir():
            continue
        for transcript in transcript_files(root):
            events.extend(
                normalized_event(event, prices)
                for event in codex_events_in_file(transcript)
            )
    events.sort(key=lambda event: str(event["timestamp"]))
    destination = normalized_events_path()
    write_private_json(destination, {"schema_version": 1, "events": events})
