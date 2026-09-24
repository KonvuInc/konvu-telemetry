"""Codex subscription-credit equivalents from recorded token usage."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .models import UsageEvent
from .pricing import is_internal_codex_review, requires_pricing


CODEX_CREDIT_EQUIVALENT_SOURCE = "https://learn.chatgpt.com/docs/pricing"
CODEX_CREDIT_RATE_CARD = "standard_token"


@dataclass(frozen=True)
class CreditRate:
    input: float
    cached_input: float
    output: float


# Credits per million tokens at Standard speed, published by OpenAI.
CODEX_CREDIT_RATES: dict[str, CreditRate] = {
    "gpt-6-astra": CreditRate(250, 25, 1_250),
    "gpt-6-sol": CreditRate(50, 5, 250),
    "gpt-6-luna": CreditRate(2.5, 0.25, 12.5),
    "gpt-5.6-sol": CreditRate(100, 10, 500),
    "daybreak-blue": CreditRate(100, 10, 500),
    "daybreak-red": CreditRate(312.5, 31.25, 1_875),
    "gpt-5.6-terra": CreditRate(50, 5, 300),
    "gpt-5.6-luna": CreditRate(5, 0.5, 30),
    "gpt-rosalind-research": CreditRate(125, 12.5, 625),
    "gpt-5.5": CreditRate(125, 12.5, 750),
    "gpt-5.4-mini": CreditRate(18.75, 1.875, 113),
    "gpt-5.4": CreditRate(62.5, 6.25, 375),
}


def _canonical_model(model: str) -> str:
    value = model.strip().lower()
    for prefix in ("openai/", "chatgpt/"):
        value = value.removeprefix(prefix)
    return value


def codex_credit_rate(model: str) -> CreditRate | None:
    """Return the published standard token-credit rate for a recorded model id."""
    candidate = _canonical_model(model)
    for name in sorted(CODEX_CREDIT_RATES, key=len, reverse=True):
        if candidate != name and not candidate.startswith(f"{name}-"):
            continue
        suffix = candidate.removeprefix(name).removeprefix("-")
        if not suffix or suffix in {"codex", "codex-max", "latest"}:
            return CODEX_CREDIT_RATES[name]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", suffix):
            return CODEX_CREDIT_RATES[name]
        return None
    return None


def codex_credit_equivalent(event: UsageEvent) -> float | None:
    """Calculate the credit equivalent for one complete Codex usage event."""
    if (
        event.provider != "codex"
        or not event.usage.complete
        or is_internal_codex_review(event)
    ):
        return None
    rate = codex_credit_rate(event.model)
    if rate is None:
        return None
    model = _canonical_model(event.model)
    multiplier = (
        2.5
        if event.usage.speed == "fast"
        and model.startswith(("gpt-6-", "gpt-5.6", "gpt-5.5"))
        else 2.0
        if event.usage.speed == "fast" and model.startswith("gpt-5.4")
        else 1.0
    )
    per_million = 1_000_000
    return multiplier * (
        (event.usage.input_tokens + event.usage.cache_write_tokens)
        * rate.input
        / per_million
        + event.usage.cache_read_tokens * rate.cached_input / per_million
        + (event.usage.output_tokens + event.usage.reasoning_output_tokens)
        * rate.output
        / per_million
    )


def codex_credit_equivalent_status(events: list[UsageEvent]) -> tuple[str, int]:
    """Label a Codex credit equivalent as complete, partial, or unavailable."""
    relevant = [event for event in events if requires_pricing(event)]
    unrated = sum(1 for event in relevant if codex_credit_equivalent(event) is None)
    if not relevant:
        return "unavailable", 0
    if unrated == 0:
        return "complete", 0
    if unrated == len(relevant):
        return "unavailable", unrated
    return "partial", unrated
