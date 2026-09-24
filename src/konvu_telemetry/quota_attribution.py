"""Estimate local-session shares of provider-reported subscription use."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import TypedDict

from .storage import quota_attribution_path, write_private_json


class SessionUsage(TypedDict):
    tokens: float
    credits: float | None


MIN_FORECAST_SAMPLES = 3
MIN_FORECAST_PERCENT = 5.0
MAX_FORECAST_RATE_SPREAD = 3.0


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _load_state() -> dict[str, object]:
    try:
        state = json.loads(quota_attribution_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "providers": {}}
    if not isinstance(state, dict) or state.get("version") != 1:
        return {"version": 1, "providers": {}}
    providers = state.get("providers")
    return state if isinstance(providers, dict) else {"version": 1, "providers": {}}


def _session_usage(session: dict[str, object]) -> SessionUsage | None:
    provider = session.get("provider")
    session_id = session.get("id")
    if provider not in {"claude", "codex"} or not isinstance(session_id, str):
        return None
    token_usage = session.get("token_usage")
    tokens = 0.0
    if isinstance(token_usage, dict):
        tokens = sum(_number(token_usage.get(key)) or 0.0 for key in token_usage)
    credits = _number(session.get("total_credit_equivalent"))
    return {"tokens": tokens, "credits": credits}


def _weight(provider: str, usage: SessionUsage) -> float:
    if provider == "codex" and usage["credits"] is not None:
        return usage["credits"]
    return usage["tokens"]


def _forecast_weight(provider: str, session: dict[str, object]) -> float | None:
    if provider == "codex":
        return _number(session.get("projected_next_10_tasks_credit_equivalent"))
    return _number(session.get("projected_next_10_usage_tokens"))


def _calibration_rate(window: dict[str, object]) -> float | None:
    raw_samples = window.get("calibration_samples")
    if not isinstance(raw_samples, list):
        return None
    samples: list[tuple[float, float]] = []
    for raw_sample in raw_samples:
        if not isinstance(raw_sample, dict):
            continue
        percent = _number(raw_sample.get("percent"))
        weight = _number(raw_sample.get("weight"))
        if percent is not None and percent > 0 and weight is not None and weight > 0:
            samples.append((percent, weight))
    if len(samples) < MIN_FORECAST_SAMPLES:
        return None
    total_percent = sum(percent for percent, _ in samples)
    if total_percent < MIN_FORECAST_PERCENT:
        return None
    rates = [percent / weight for percent, weight in samples]
    if max(rates) / min(rates) > MAX_FORECAST_RATE_SPREAD:
        return None
    return total_percent / sum(weight for _, weight in samples)


def _window_key(window: dict[str, object]) -> str | None:
    period = window.get("period")
    limit_id = window.get("limit_id", "default")
    reset = window.get("resets_at")
    if not isinstance(period, str) or not isinstance(limit_id, str):
        return None
    reset_key = "unknown"
    if isinstance(reset, str):
        try:
            reset_key = (
                datetime.fromisoformat(reset.replace("Z", "+00:00"))
                .replace(second=0, microsecond=0)
                .isoformat()
            )
        except ValueError:
            reset_key = reset
    return f"{limit_id}:{period}:{reset_key}"


def apply_quota_attribution(snapshot: dict[str, object]) -> None:
    """Attach forward-only quota-share estimates and persist their small local ledger."""
    sessions = snapshot.get("sessions")
    quotas = snapshot.get("account_quotas")
    if not isinstance(sessions, list) or not isinstance(quotas, dict):
        return
    state = _load_state()
    providers = state["providers"]
    assert isinstance(providers, dict)
    session_rows = [row for row in sessions if isinstance(row, dict)]
    for provider in ("claude", "codex"):
        account = quotas.get(provider)
        if not isinstance(account, dict):
            continue
        raw_windows = account.get("windows")
        if not isinstance(raw_windows, list):
            continue
        provider_state = providers.setdefault(provider, {"sessions": {}, "windows": {}})
        if not isinstance(provider_state, dict):
            providers[provider] = provider_state = {"sessions": {}, "windows": {}}
        previous_sessions = provider_state.setdefault("sessions", {})
        pending = provider_state.setdefault("pending", {})
        windows_state = provider_state.setdefault("windows", {})
        if (
            not isinstance(previous_sessions, dict)
            or not isinstance(pending, dict)
            or not isinstance(windows_state, dict)
        ):
            providers[provider] = provider_state = {
                "sessions": {},
                "pending": {},
                "windows": {},
            }
            previous_sessions = provider_state["sessions"]
            pending = provider_state["pending"]
            windows_state = provider_state["windows"]
        current: dict[str, SessionUsage] = {}
        for session in session_rows:
            if session.get("provider") != provider or not isinstance(
                session.get("id"), str
            ):
                continue
            usage = _session_usage(session)
            if usage is None:
                continue
            key = str(session["id"])
            current[key] = usage
            earlier = previous_sessions.get(key)
            if isinstance(earlier, dict):
                old_tokens = _number(earlier.get("tokens")) or 0.0
                old_credits = _number(earlier.get("credits"))
                token_delta = max(0.0, usage["tokens"] - old_tokens)
                credit_delta = (
                    max(0.0, usage["credits"] - old_credits)
                    if usage["credits"] is not None and old_credits is not None
                    else None
                )
                existing = pending.get(key)
                total = existing if isinstance(existing, dict) else {}
                total["tokens"] = (_number(total.get("tokens")) or 0.0) + token_delta
                if credit_delta is not None:
                    total["credits"] = (
                        _number(total.get("credits")) or 0.0
                    ) + credit_delta
                pending[key] = total
        provider_state["sessions"] = current
        interval_weights = {
            session_id: _weight(
                provider,
                {
                    "tokens": _number(value.get("tokens")) or 0.0,
                    "credits": _number(value.get("credits")),
                },
            )
            for session_id, value in pending.items()
            if isinstance(value, dict)
        }
        interval_weights = {
            session_id: weight
            for session_id, weight in interval_weights.items()
            if weight > 0
        }
        total_weight = sum(interval_weights.values())
        for raw_window in raw_windows:
            if not isinstance(raw_window, dict):
                continue
            window_key = _window_key(raw_window)
            used = _number(raw_window.get("used_percent"))
            if window_key is None or used is None:
                continue
            old_window = windows_state.get(window_key)
            if not isinstance(old_window, dict):
                windows_state[window_key] = {"used_percent": used, "allocations": {}}
                continue
            old_window.pop("rates", None)
            previous_used = _number(old_window.get("used_percent"))
            if previous_used is None:
                old_window["used_percent"] = used
                continue
            if used < previous_used:
                # A reset starts a fresh provider window; old shares must not leak into it.
                old_window["used_percent"] = used
                old_window["allocations"] = {}
                old_window["calibration_samples"] = []
                continue
            if used == previous_used:
                continue
            increase = used - previous_used
            allocations = old_window.setdefault("allocations", {})
            if isinstance(allocations, dict) and total_weight > 0:
                for session_id, weight in interval_weights.items():
                    allocations[session_id] = (
                        _number(allocations.get(session_id)) or 0.0
                    ) + increase * weight / total_weight
                samples = old_window.setdefault("calibration_samples", [])
                if isinstance(samples, list):
                    samples.append({"percent": increase, "weight": total_weight})
                    old_window["calibration_samples"] = samples[-8:]
            old_window["used_percent"] = used
        # One interval is allocated independently to every provider window.
        provider_state["pending"] = {}
        for session in session_rows:
            if session.get("provider") != provider or not isinstance(
                session.get("id"), str
            ):
                continue
            estimates: list[dict[str, object]] = []
            for raw_window in raw_windows:
                if not isinstance(raw_window, dict):
                    continue
                window_key = _window_key(raw_window)
                stored = (
                    windows_state.get(window_key) if window_key is not None else None
                )
                allocations = (
                    stored.get("allocations") if isinstance(stored, dict) else None
                )
                share = (
                    _number(allocations.get(session["id"]))
                    if isinstance(allocations, dict)
                    else None
                )
                if share is not None:
                    estimate: dict[str, object] = {
                        "period": raw_window.get("period"),
                        "estimated_percent": round(share, 2),
                    }
                    forecast_weight = _forecast_weight(provider, session)
                    calibration = (
                        _calibration_rate(stored) if isinstance(stored, dict) else None
                    )
                    if forecast_weight is not None and calibration is not None:
                        estimate["projected_next_10_percent"] = round(
                            calibration * forecast_weight, 2
                        )
                    estimates.append(estimate)
            session["quota_attribution"] = {
                "state": "observing" if not estimates else "estimated",
                "windows": estimates,
            }
    write_private_json(quota_attribution_path(), state)


def apply_usage_modes(snapshot: dict[str, object]) -> None:
    """Mark each session as included, exhausted, API-billed, or unknown."""
    sessions = snapshot.get("sessions")
    quotas = snapshot.get("account_quotas")
    if not isinstance(sessions, list):
        return
    for session in sessions:
        if not isinstance(session, dict):
            continue
        provider = session.get("provider")
        account = quotas.get(provider) if isinstance(quotas, dict) else None
        windows = account.get("windows") if isinstance(account, dict) else None
        exhausted = (
            isinstance(account, dict) and account.get("ordinary_usage_allowed") is False
        )
        if isinstance(windows, list):
            exhausted = exhausted or any(
                (_number(window.get("used_percent")) or 0.0) >= 100
                for window in windows
                if isinstance(window, dict)
                and window.get("period") in {"five_hour", "weekly"}
            )
        if exhausted:
            session["usage_mode"] = "exhausted"
        elif isinstance(windows, list) and windows:
            session["usage_mode"] = "included"
        else:
            session["usage_mode"] = "unknown"


def apply_out_of_plan_accounting(snapshot: dict[str, object]) -> None:
    """Count exhausted-plan spend only from the first locally observed cutoff."""
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list):
        return
    state = _load_state()
    providers = state["providers"]
    assert isinstance(providers, dict)
    for provider in ("claude", "codex"):
        rows = [
            session
            for session in sessions
            if isinstance(session, dict) and session.get("provider") == provider
        ]
        if not rows:
            continue
        provider_state = providers.setdefault(provider, {"sessions": {}, "windows": {}})
        if not isinstance(provider_state, dict):
            providers[provider] = provider_state = {"sessions": {}, "windows": {}}
        billing = provider_state.setdefault("billing", {})
        if not isinstance(billing, dict):
            provider_state["billing"] = billing = {}
        exhausted = all(session.get("usage_mode") == "exhausted" for session in rows)
        if not exhausted:
            billing["mode"] = "included"
            billing["offsets"] = {}
            continue
        offsets = billing.setdefault("offsets", {})
        if not isinstance(offsets, dict):
            billing["offsets"] = offsets = {}
        for session in rows:
            session_id = session.get("id")
            total = _number(session.get("total_cost_usd"))
            if not isinstance(session_id, str) or total is None:
                continue
            prior = _number(offsets.get(session_id))
            if prior is None:
                offsets[session_id] = total
                prior = total
            session["out_of_plan_spend_usd"] = round(max(0.0, total - prior), 6)
            session["out_of_plan_spend_status"] = "since_observed_plan_exit"
        billing["mode"] = "exhausted"
    write_private_json(quota_attribution_path(), state)
