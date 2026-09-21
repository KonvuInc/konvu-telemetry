"""Local model pricing and cost completeness rules."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from threading import Lock

from .models import UsageEvent

_PRICING_CACHE: tuple[Path, int, int, int, dict[str, dict[str, float]]] | None = None
_PRICING_CACHE_LOCK = Lock()


def pricing_path() -> Path:
    """Return an optional user-managed price-table override."""
    configured = os.environ.get("KONVU_TELEMETRY_PRICING_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).with_name("pricing.json")


def normalised_model(model: str) -> str:
    return model.lower().removeprefix("anthropic/")


def nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def load_pricing() -> dict[str, dict[str, float]]:
    """Load Konvu's bundled LiteLLM price snapshot or an explicit local override."""
    global _PRICING_CACHE
    path = pricing_path()
    try:
        stat = path.stat()
    except OSError:
        return {}
    cache = _PRICING_CACHE
    cache_key = (path, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    if cache is not None and cache[:4] == cache_key:
        return cache[4]
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data", raw)
    if not isinstance(data, dict):
        return {}
    prices: dict[str, dict[str, float]] = {}
    for model, values in data.items():
        if not isinstance(model, str) or not isinstance(values, dict):
            continue
        camel_case = "inputCostPerToken" in values
        if camel_case:
            input_rate = values.get("inputCostPerToken")
            output_rate = values.get("outputCostPerToken")
            cache_write_rate = values.get("cacheWriteCostPerToken")
            cache_read_rate = values.get("cacheReadCostPerToken")
        else:
            input_rate = values.get("input_cost_per_token")
            output_rate = values.get("output_cost_per_token")
            cache_write_rate = values.get("cache_creation_input_token_cost")
            cache_read_rate = values.get("cache_read_input_token_cost")
        input_number = nonnegative_number(input_rate)
        output_number = nonnegative_number(output_rate)
        if input_number is None or output_number is None:
            continue
        web_search_rate = values.get(
            "webSearchCostPerRequest",
            values.get(
                "web_search_cost_per_request",
                values.get("search_context_cost_per_query", 0.01),
            ),
        )
        if isinstance(web_search_rate, dict):
            web_search_rate = web_search_rate.get("search_context_size_medium", 0.01)
        web_search_number = nonnegative_number(web_search_rate)
        fast_multiplier = nonnegative_number(values.get("fastMultiplier", 1.0))
        context_window = nonnegative_number(values.get("max_input_tokens", 0))
        cache_write_number = nonnegative_number(cache_write_rate)
        cache_read_number = nonnegative_number(cache_read_rate)
        price = {
            "input": input_number,
            "output": output_number,
            "cache_write": cache_write_number
            if cache_write_number is not None
            else input_number * 1.25,
            "cache_read": cache_read_number
            if cache_read_number is not None
            else input_number * 0.1,
            "web_search": web_search_number if web_search_number is not None else 0.01,
            "fast_multiplier": fast_multiplier
            if fast_multiplier is not None and fast_multiplier > 0
            else 1.0,
            "context_window_tokens": context_window
            if context_window is not None
            else 0.0,
            "long_context_threshold": 0.0,
        }
        for field, key in (
            ("input", "input_cost_per_token"),
            ("output", "output_cost_per_token"),
            ("cache_write", "cache_creation_input_token_cost"),
            ("cache_read", "cache_read_input_token_cost"),
        ):
            camel_key = "".join(
                part.title() if index else part
                for index, part in enumerate(key.split("_"))
            )
            key_variants = (key, camel_key) if camel_case else (key,)
            for suffix, label in (
                ("_priority", "priority"),
                ("_flex", "flex"),
                ("_above_200k_tokens", "long"),
                ("_above_272k_tokens", "long"),
                ("_above_200k_tokens_priority", "long_priority"),
                ("_above_272k_tokens_priority", "long_priority"),
                ("_above_200k_tokens_flex", "long_flex"),
                ("_above_272k_tokens_flex", "long_flex"),
            ):
                for variant in key_variants:
                    value = values.get(variant + suffix)
                    number = nonnegative_number(value)
                    if number is None:
                        continue
                    price[f"{field}_{label}"] = number
                    if "above_200k" in suffix:
                        price["long_context_threshold"] = 200_000.0
                    elif "above_272k" in suffix and not price["long_context_threshold"]:
                        price["long_context_threshold"] = 272_000.0
                    break
        prices[normalised_model(model)] = price
    with _PRICING_CACHE_LOCK:
        _PRICING_CACHE = (*cache_key, prices)
    return prices


def price_for(
    model: str, prices: dict[str, dict[str, float]]
) -> dict[str, float] | None:
    candidate = normalised_model(model)
    return prices.get(candidate)


def claude_context_window(
    model: str,
    prices: dict[str, dict[str, float]] | None = None,
) -> int | None:
    """Return the model's published input-context capacity when known."""
    rates = price_for(model, prices or {})
    advertised = rates.get("context_window_tokens") if rates else 0
    return (
        int(advertised)
        if isinstance(advertised, (int, float))
        and math.isfinite(advertised)
        and advertised >= 1
        else None
    )


def cache_read_rate(model: str, prices: dict[str, dict[str, float]]) -> float | None:
    """Dollars per million cached input tokens, the floor a turn pays to re-send its context."""
    price = price_for(model, prices)
    rate = price.get("cache_read") if price else None
    return (
        round(rate * 1_000_000, 6)
        if isinstance(rate, (int, float)) and rate > 0
        else None
    )


def event_cost(event: UsageEvent, prices: dict[str, dict[str, float]]) -> float | None:
    rates = price_for(event.model, prices)
    if rates is None:
        return None
    threshold = rates.get("long_context_threshold", 0)
    long_context = (
        isinstance(threshold, (int, float))
        and threshold > 0
        and event.usage.context_tokens > threshold
    )

    def rate_for(field: str) -> float:
        long_rate = rates.get(f"{field}_long") if long_context else None
        base = float(long_rate) if isinstance(long_rate, (int, float)) else rates[field]
        if event.usage.speed == "fast":
            premium = rates.get(
                f"{field}_long_priority" if long_context else f"{field}_priority"
            )
            return (
                float(premium)
                if isinstance(premium, (int, float))
                else base * fast_multiplier(event, rates)
            )
        if event.usage.speed == "flex":
            flexible = rates.get(
                f"{field}_long_flex" if long_context else f"{field}_flex"
            )
            return float(flexible) if isinstance(flexible, (int, float)) else base
        return base

    input_rate = rate_for("input")
    output_rate = rate_for("output")
    cache_write_rate = rate_for("cache_write")
    cache_read_rate = rate_for("cache_read")
    five_minute_cache_write = max(
        0, event.usage.cache_write_tokens - event.usage.cache_write_one_hour_tokens
    )
    return (
        event.usage.input_tokens * input_rate
        + event.usage.output_tokens * output_rate
        + five_minute_cache_write * cache_write_rate
        + event.usage.cache_write_one_hour_tokens * cache_write_rate * 1.6
        + event.usage.cache_read_tokens * cache_read_rate
        + event.usage.web_search_requests * rates.get("web_search", 0.01)
    )


def is_internal_codex_review(event: UsageEvent) -> bool:
    """Return whether an event belongs to Codex's unpriced internal review worker."""
    return (
        event.provider == "codex"
        and normalised_model(event.model) == "codex-auto-review"
    )


def requires_pricing(event: UsageEvent) -> bool:
    """Return whether an event can contribute a nonzero cost to its session."""
    return not is_internal_codex_review(event) and (
        event.usage.input_tokens > 0
        or event.usage.output_tokens > 0
        or event.usage.cache_write_tokens > 0
        or event.usage.cache_read_tokens > 0
        or event.usage.web_search_requests > 0
    )


def cost_status(
    events: list[UsageEvent], prices: dict[str, dict[str, float]]
) -> tuple[str, int]:
    """Label a session cost as complete, partial, or unavailable."""
    billable_events = [event for event in events if requires_pricing(event)]
    unpriced = sum(1 for event in billable_events if event_cost(event, prices) is None)
    if not billable_events:
        return "unavailable", 0
    if unpriced == 0:
        return "complete", 0
    if unpriced == len(billable_events):
        return "unavailable", unpriced
    return "partial", unpriced


def fast_multiplier(event: UsageEvent, rates: dict[str, float]) -> float:
    """Return the documented Fast multiplier for a priced local model event."""
    if event.usage.speed != "fast":
        return 1.0
    model = normalised_model(event.model)
    if event.provider == "codex":
        if model.startswith(("gpt-5.6", "gpt-5.5")):
            return 2.5
        if model.startswith("gpt-5.4"):
            return 2.0
    return rates.get("fast_multiplier", 1.0)
