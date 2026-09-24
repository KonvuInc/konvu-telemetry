"""Codex subscription billing metadata, quota, and credit estimates."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from threading import Lock
import time
from typing import Literal, TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import UsageEvent
from .pricing import is_internal_codex_review, requires_pricing


CODEX_CREDIT_SOURCE = "https://learn.chatgpt.com/docs/pricing"
CODEX_USAGE_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
CODEX_USAGE_CACHE_SECONDS = 60.0
CODEX_USAGE_TIMEOUT_SECONDS = 2.0
MAX_AUTH_BYTES = 256 * 1024
MAX_USAGE_BYTES = 1024 * 1024


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


BillingMode = Literal["chatgpt_subscription", "api_key", "unknown"]


class CodexAccount(TypedDict):
    billing_mode: BillingMode
    plan_type: str | None
    plan_label: str | None


_USAGE_CACHE_LOCK = Lock()
_USAGE_CACHE: tuple[float, str, dict[str, object] | None] | None = None


def codex_auth_path() -> Path:
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return root / "auth.json"


def _mapping(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _auth_document(path: Path | None = None) -> dict[str, object] | None:
    source = path or codex_auth_path()
    try:
        if source.stat().st_size > MAX_AUTH_BYTES:
            return None
        decoded: object = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return _mapping(decoded)


def _jwt_payload(value: object) -> dict[str, object] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded: object = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    return _mapping(decoded)


def _safe_plan_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    plan = value.strip().lower()
    return (
        plan
        if plan and len(plan) <= 96 and re.fullmatch(r"[a-z0-9_-]+", plan)
        else None
    )


def plan_label(plan_type: str | None) -> str | None:
    if plan_type is None:
        return None
    normalized = re.sub(r"[_-]usage[_-]based$", "", plan_type)
    normalized = re.sub(r"^self[_-]serve[_-]", "", normalized)
    normalized = re.sub(r"[_-]cbp$", "", normalized)
    labels = {
        "free": "Free",
        "go": "Go",
        "plus": "Plus",
        "pro": "Pro",
        "prolite": "Pro Lite",
        "pro_lite": "Pro Lite",
        "team": "Team",
        "business": "Business",
        "business_prolite": "Business Pro Lite",
        "enterprise": "Enterprise",
        "education": "Education",
        "edu": "Edu",
    }
    return labels.get(normalized, re.sub(r"[_-]+", " ", normalized).title())


def codex_account(path: Path | None = None) -> CodexAccount:
    """Read the current billing mode and subscription type without returning credentials."""
    document = _auth_document(path)
    if document is None:
        return {"billing_mode": "unknown", "plan_type": None, "plan_label": None}
    raw_mode = document.get("auth_mode")
    mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
    billing_mode: BillingMode = (
        "chatgpt_subscription"
        if mode == "chatgpt"
        else "api_key"
        if mode in {"api_key", "apikey"}
        else "unknown"
    )
    tokens = _mapping(document.get("tokens"))
    claims = _jwt_payload(tokens.get("id_token")) if tokens is not None else None
    auth_claims = (
        _mapping(claims.get("https://api.openai.com/auth"))
        if claims is not None
        else None
    )
    plan_type = _safe_plan_type(
        auth_claims.get("chatgpt_plan_type") if auth_claims is not None else None
    )
    return {
        "billing_mode": billing_mode,
        "plan_type": plan_type,
        "plan_label": plan_label(plan_type),
    }


def _canonical_model(model: str) -> str:
    value = model.strip().lower()
    for prefix in ("openai/", "chatgpt/"):
        value = value.removeprefix(prefix)
    return value


def codex_credit_rate(model: str) -> CreditRate | None:
    """Return the official subscription-credit rate for a recorded model id."""
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


def codex_credits(event: UsageEvent) -> float | None:
    """Calculate subscription credits for one complete Codex usage event."""
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


def codex_credit_status(events: list[UsageEvent]) -> tuple[str, int]:
    """Label a Codex credit estimate as complete, partial, or unavailable."""
    relevant = [event for event in events if requires_pricing(event)]
    unrated = sum(1 for event in relevant if codex_credits(event) is None)
    if not relevant:
        return "unavailable", 0
    if unrated == 0:
        return "complete", 0
    if unrated == len(relevant):
        return "unavailable", unrated
    return "partial", unrated


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _iso_epoch(value: object) -> str | None:
    number = _number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _usage_window(
    value: object, observed_at: str, limit_id: str = "codex"
) -> dict[str, object] | None:
    row = _mapping(value)
    if row is None:
        return None
    used = _number(row.get("used_percent"))
    seconds = _number(row.get("limit_window_seconds"))
    if used is None or not 0 <= used <= 100 or seconds is None or seconds <= 0:
        return None
    return {
        "limit_id": limit_id,
        "window_minutes": seconds / 60,
        "used_percent": used,
        "remaining_percent": 100 - used,
        "resets_at": _iso_epoch(row.get("reset_at")),
        "observed_at": observed_at,
    }


def normalize_codex_usage(
    body: object, observed_at: str, account: CodexAccount
) -> dict[str, object] | None:
    """Normalize the authenticated Codex usage response without retaining credentials."""
    data = _mapping(body)
    if data is None:
        return None
    rate_limit = _mapping(data.get("rate_limit"))
    windows: list[dict[str, object]] = []
    if rate_limit is not None:
        for name in ("primary_window", "secondary_window"):
            window = _usage_window(rate_limit.get(name), observed_at)
            if window is not None:
                windows.append(window)
    additional = data.get("additional_rate_limits")
    for item in additional if isinstance(additional, list) else []:
        row = _mapping(item)
        nested = _mapping(row.get("rate_limit")) if row is not None else None
        limit_name = row.get("limit_name") if row is not None else None
        if nested is None or not isinstance(limit_name, str):
            continue
        for name in ("primary_window", "secondary_window"):
            window = _usage_window(nested.get(name), observed_at, limit_name)
            if window is not None:
                windows.append(window)
    result: dict[str, object] = {
        "observed_at": observed_at,
        "source": "codex_usage_endpoint",
        "windows": windows,
        **account,
    }
    plan_type = _safe_plan_type(data.get("plan_type")) or account["plan_type"]
    result["plan_type"] = plan_type
    result["plan_label"] = plan_label(plan_type)
    credits = _mapping(data.get("credits"))
    normalized_credits: dict[str, object] = {}
    if credits is not None:
        normalized_credits = {
            key: value
            for key, value in (
                ("has_credits", credits.get("has_credits")),
                ("unlimited", credits.get("unlimited")),
                ("balance", _number(credits.get("balance"))),
            )
            if isinstance(value, bool) or isinstance(value, (int, float))
        }
        if normalized_credits:
            result["credits"] = normalized_credits
    if "rate_limit_reached_type" in data:
        reached = data["rate_limit_reached_type"]
        if isinstance(reached, str) or reached is None:
            result["rate_limit_reached_type"] = reached
    return (
        result
        if windows or normalized_credits or "rate_limit_reached_type" in result
        else None
    )


def fetch_codex_usage(
    now: float | None = None,
    auth_path: Path | None = None,
    timeout: float = CODEX_USAGE_TIMEOUT_SECONDS,
) -> dict[str, object] | None:
    """Fetch current Codex quota and credit state with the locally stored OAuth token."""
    global _USAGE_CACHE
    current = time.time() if now is None else now
    document = _auth_document(auth_path)
    account = codex_account(auth_path)
    result: dict[str, object] | None = None
    cache_key = "no-chatgpt-token"
    if document is not None and account["billing_mode"] == "chatgpt_subscription":
        tokens = _mapping(document.get("tokens"))
        access_token = tokens.get("access_token") if tokens is not None else None
        account_id = tokens.get("account_id") if tokens is not None else None
        if isinstance(access_token, str) and access_token:
            cache_key = hashlib.sha256(
                f"{access_token}\0{account_id or ''}".encode()
            ).hexdigest()
            if auth_path is None:
                with _USAGE_CACHE_LOCK:
                    if (
                        _USAGE_CACHE is not None
                        and _USAGE_CACHE[1] == cache_key
                        and 0 <= current - _USAGE_CACHE[0] < CODEX_USAGE_CACHE_SECONDS
                    ):
                        return _USAGE_CACHE[2]
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": "konvu-telemetry",
            }
            if isinstance(account_id, str) and account_id:
                headers["ChatGPT-Account-Id"] = account_id
            request = Request(CODEX_USAGE_ENDPOINT, headers=headers, method="GET")
            try:
                with urlopen(request, timeout=timeout) as response:
                    raw = response.read(MAX_USAGE_BYTES + 1)
                if len(raw) <= MAX_USAGE_BYTES:
                    payload: object = json.loads(raw)
                    observed = datetime.fromtimestamp(current, timezone.utc).isoformat()
                    result = normalize_codex_usage(payload, observed, account)
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
                result = None
    if auth_path is None:
        with _USAGE_CACHE_LOCK:
            _USAGE_CACHE = (current, cache_key, result)
    return result
