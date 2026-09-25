"""Core accounting types shared across the collector."""

from __future__ import annotations

from dataclasses import dataclass


# A cached read costs a tenth of a fresh input token, the same ratio pricing.py
# falls back to. Counting it one-for-one let cache reads — which re-read the
# whole conversation every prompt — dominate quota weighting in long sessions.
CACHE_READ_QUOTA_WEIGHT = 0.1


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_write_one_hour_tokens: int
    cache_read_tokens: int
    web_search_requests: int
    speed: str
    reasoning_output_tokens: int = 0
    complete: bool = True

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_output_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens
        )

    @property
    def quota_tokens(self) -> float:
        """Tokens weighted by what they actually cost against a quota."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_output_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens * CACHE_READ_QUOTA_WEIGHT
        )

    @property
    def context_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


@dataclass(frozen=True)
class UsageEvent:
    provider: str
    session_id: str
    message_id: str | None
    timestamp: float
    model: str
    usage: Usage
    tool_calls: int
    is_subagent: bool
    agent_id: str | None
    effort: str
    context_window_tokens: int | None = None
