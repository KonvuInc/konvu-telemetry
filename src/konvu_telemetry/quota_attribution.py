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


class StoredSessionUsage(SessionUsage, total=False):
    last_seen_at: float


MIN_FORECAST_SAMPLES = 1
MIN_FORECAST_PERCENT = 1.0
SESSION_BASELINE_RETENTION_SECONDS = 32 * 24 * 60 * 60
STATE_VERSION = 2


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _load_state() -> dict[str, object]:
    try:
        state = json.loads(quota_attribution_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "providers": {}}
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        return {"version": STATE_VERSION, "providers": {}}
    providers = state.get("providers")
    return (
        state
        if isinstance(providers, dict)
        else {"version": STATE_VERSION, "providers": {}}
    )


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
    return total_percent / sum(weight for _, weight in samples)


def _snapshot_time(snapshot: dict[str, object]) -> float | None:
    value = snapshot.get("generated_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
    observed_at = _snapshot_time(snapshot)
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
        windows_state = provider_state.setdefault("windows", {})
        if not isinstance(previous_sessions, dict) or not isinstance(
            windows_state, dict
        ):
            providers[provider] = provider_state = {
                "sessions": {},
                "windows": {},
            }
            previous_sessions = provider_state["sessions"]
            windows_state = provider_state["windows"]
        provider_state.pop("pending", None)
        current: dict[str, StoredSessionUsage] = {}
        interval_weights: dict[str, float] = {}
        for session in session_rows:
            if session.get("provider") != provider or not isinstance(
                session.get("id"), str
            ):
                continue
            usage = _session_usage(session)
            if usage is None:
                continue
            key = str(session["id"])
            stored_usage: StoredSessionUsage = {
                "tokens": usage["tokens"],
                "credits": usage["credits"],
            }
            if observed_at is not None:
                stored_usage["last_seen_at"] = observed_at
            current[key] = stored_usage
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
                weight = _weight(
                    provider,
                    {"tokens": token_delta, "credits": credit_delta},
                )
                if weight > 0:
                    interval_weights[key] = weight
        retained_sessions = {
            session_id: usage
            for session_id, usage in previous_sessions.items()
            if isinstance(session_id, str)
            and isinstance(usage, dict)
            and session_id not in current
            and (
                observed_at is None
                or (last_seen := _number(usage.get("last_seen_at"))) is None
                or observed_at - last_seen <= SESSION_BASELINE_RETENTION_SECONDS
            )
        }
        provider_state["sessions"] = {**retained_sessions, **current}
        for raw_window in raw_windows:
            if not isinstance(raw_window, dict):
                continue
            window_key = _window_key(raw_window)
            used = _number(raw_window.get("used_percent"))
            if window_key is None or used is None:
                continue
            old_window = windows_state.get(window_key)
            if not isinstance(old_window, dict):
                windows_state[window_key] = {
                    "used_percent": used,
                    "allocations": {},
                    "pending": {},
                }
                continue
            old_window.pop("rates", None)
            previous_used = _number(old_window.get("used_percent"))
            if previous_used is None:
                old_window["used_percent"] = used
                old_window["pending"] = {}
                continue
            if used < previous_used:
                # A reset starts a fresh provider window; old shares must not leak into it.
                old_window["used_percent"] = used
                old_window["allocations"] = {}
                old_window["calibration_samples"] = []
                old_window["pending"] = {}
                continue
            pending = old_window.setdefault("pending", {})
            if not isinstance(pending, dict):
                old_window["pending"] = pending = {}
            for session_id, weight in interval_weights.items():
                pending[session_id] = (_number(pending.get(session_id)) or 0.0) + weight
            if used == previous_used:
                continue
            increase = used - previous_used
            allocations = old_window.setdefault("allocations", {})
            total_weight = sum(_number(weight) or 0.0 for weight in pending.values())
            if isinstance(allocations, dict) and total_weight > 0:
                for session_id, raw_weight in pending.items():
                    weight = _number(raw_weight) or 0.0
                    allocations[session_id] = (
                        _number(allocations.get(session_id)) or 0.0
                    ) + increase * weight / total_weight
                samples = old_window.setdefault("calibration_samples", [])
                if isinstance(samples, list):
                    samples.append({"percent": increase, "weight": total_weight})
                    old_window["calibration_samples"] = samples[-8:]
            old_window["pending"] = {}
            old_window["used_percent"] = used
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
                used_percent = _number(raw_window.get("used_percent"))
                if not isinstance(stored, dict) or used_percent is None:
                    continue
                allocations = stored.get("allocations")
                confirmed_share = (
                    _number(allocations.get(session["id"]))
                    if isinstance(allocations, dict)
                    else None
                )
                calibration = _calibration_rate(stored)
                pending = stored.get("pending")
                pending_weight = (
                    _number(pending.get(session["id"]))
                    if isinstance(pending, dict)
                    else None
                )
                provisional_share = 0.0
                if (
                    calibration is not None
                    and pending_weight is not None
                    and isinstance(pending, dict)
                ):
                    confirmed_total = (
                        sum(_number(value) or 0.0 for value in allocations.values())
                        if isinstance(allocations, dict)
                        else 0.0
                    )
                    pending_total = sum(
                        _number(value) or 0.0 for value in pending.values()
                    )
                    provisional_total = calibration * pending_total
                    headroom = max(0.0, used_percent - confirmed_total)
                    scale = (
                        min(1.0, headroom / provisional_total)
                        if provisional_total > 0
                        else 0.0
                    )
                    provisional_share = calibration * pending_weight * scale
                share = (confirmed_share or 0.0) + provisional_share
                forecast_weight = _forecast_weight(provider, session)
                if share > 0 or (
                    forecast_weight is not None and calibration is not None
                ):
                    estimate: dict[str, object] = {
                        "period": raw_window.get("period"),
                        "estimated_percent": round(share, 2),
                        "scope": "observed_window",
                    }
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
        status = account.get("status") if isinstance(account, dict) else None
        session["quota_status"] = (
            status
            if status in {"stale", "unavailable"}
            else "fresh"
            if isinstance(windows, list) and windows
            else "unavailable"
        )
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
