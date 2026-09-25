"""Estimate local-session shares of provider-reported subscription use."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import TypedDict

from .storage import quota_attribution_path, write_private_json


class SessionUsage(TypedDict):
    cost: float
    credits: float | None


class StoredSessionUsage(SessionUsage, total=False):
    last_seen_at: float


MIN_FORECAST_SAMPLES = 1
MIN_FORECAST_PERCENT = 1.0
RESET_TIMESTAMP_JITTER_SECONDS = 60.0
LEGACY_RESET_MATCH_SECONDS = 2 * RESET_TIMESTAMP_JITTER_SECONDS
SESSION_BASELINE_RETENTION_SECONDS = 32 * 24 * 60 * 60
# 3: Session weight changed from raw token totals to recorded cost.
# 4: Quota-window identity stopped including mutable reset timestamps.
STATE_VERSION = 4


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
    if not isinstance(state, dict) or state.get("version") not in {3, STATE_VERSION}:
        return {"version": STATE_VERSION, "providers": {}}
    providers = state.get("providers")
    if not isinstance(providers, dict):
        return {"version": STATE_VERSION, "providers": {}}
    state["version"] = STATE_VERSION
    return state


def _session_usage(session: dict[str, object]) -> SessionUsage | None:
    """Weigh a session by recorded cost, not by a raw token count.

    Summing every token bucket at parity made cache reads — which re-read the
    whole conversation on every prompt — dominate the weight, so the calibrated
    rate drifted with conversation length instead of tracking quota. Cost is
    already priced per token type and per model, so it is the closest proxy we
    hold for what a prompt actually consumes. Codex keeps credits, which are
    what its quota is denominated in.
    """
    provider = session.get("provider")
    session_id = session.get("id")
    if provider not in {"claude", "codex"} or not isinstance(session_id, str):
        return None
    if session.get("cost_status") == "unavailable":
        return None
    cost = _number(session.get("total_cost_usd")) or 0.0
    credits = _number(session.get("total_credit_equivalent"))
    return {"cost": cost, "credits": credits}


def _weight(provider: str, usage: SessionUsage) -> float:
    if provider == "codex" and usage["credits"] is not None:
        return usage["credits"]
    return usage["cost"]


def _forecast_weight(provider: str, session: dict[str, object]) -> float | None:
    """The projection must be measured the same way as the calibration weight,
    or the rate and the number it multiplies are in different units."""
    if provider == "codex":
        return _number(session.get("projected_next_10_tasks_credit_equivalent"))
    return _number(session.get("projected_next_10_tasks_usd"))


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
    if not isinstance(period, str) or not isinstance(limit_id, str):
        return None
    window_minutes = _number(window.get("window_minutes"))
    return json.dumps([limit_id, period, window_minutes], separators=(",", ":"))


def _reset_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _legacy_window_prefix(window: dict[str, object]) -> str | None:
    period = window.get("period")
    limit_id = window.get("limit_id", "default")
    if not isinstance(period, str) or not isinstance(limit_id, str):
        return None
    return f"{limit_id}:{period}:"


def _window_evidence(window: dict[str, object]) -> tuple[int, int, int]:
    allocations = window.get("allocations")
    pending = window.get("pending")
    samples = window.get("calibration_samples")
    return (
        len(samples) if isinstance(samples, list) else 0,
        len(allocations) if isinstance(allocations, dict) else 0,
        len(pending) if isinstance(pending, dict) else 0,
    )


def _stored_window(
    windows: dict[str, object], raw_window: dict[str, object]
) -> tuple[str | None, dict[str, object] | None]:
    window_key = _window_key(raw_window)
    if window_key is None:
        return None, None
    stored = windows.get(window_key)
    prefix = _legacy_window_prefix(raw_window)
    legacy = [
        (key, value)
        for key, value in windows.items()
        if isinstance(key, str)
        and prefix is not None
        and key.startswith(prefix)
        and isinstance(value, dict)
    ]
    if not isinstance(stored, dict) and legacy:
        current_reset = _reset_timestamp(raw_window.get("resets_at"))
        candidates: list[tuple[str, dict[str, object]]] = []
        for item in legacy:
            if current_reset is None:
                candidates.append(item)
                continue
            legacy_reset = (
                _reset_timestamp(item[0][len(prefix) :]) if prefix is not None else None
            )
            if (
                legacy_reset is not None
                and abs(current_reset - legacy_reset) <= LEGACY_RESET_MATCH_SECONDS
            ):
                candidates.append(item)
        if candidates:
            legacy_key, stored = max(
                candidates, key=lambda item: _window_evidence(item[1])
            )
            reset = raw_window.get("resets_at")
            if isinstance(reset, str):
                stored["resets_at"] = reset
            elif prefix is not None:
                legacy_reset_value = legacy_key[len(prefix) :]
                if _reset_timestamp(legacy_reset_value) is not None:
                    stored["resets_at"] = legacy_reset_value
            windows[window_key] = stored
    for legacy_key, _ in legacy:
        windows.pop(legacy_key, None)
    return window_key, stored if isinstance(stored, dict) else None


def _reset_advanced(
    stored: dict[str, object],
    raw_window: dict[str, object],
    observed_at: float | None,
) -> bool:
    previous = _reset_timestamp(stored.get("resets_at"))
    current = _reset_timestamp(raw_window.get("resets_at"))
    return (
        previous is not None
        and current is not None
        and current - previous > RESET_TIMESTAMP_JITTER_SECONDS
        and (
            observed_at is None
            or observed_at >= previous - RESET_TIMESTAMP_JITTER_SECONDS
        )
    )


def _refresh_reset_timestamp(
    stored: dict[str, object], raw_window: dict[str, object]
) -> None:
    previous = _reset_timestamp(stored.get("resets_at"))
    current_value = raw_window.get("resets_at")
    current = _reset_timestamp(current_value)
    if isinstance(current_value, str) and (
        previous is None
        or (
            current is not None
            and abs(current - previous) <= RESET_TIMESTAMP_JITTER_SECONDS
        )
    ):
        stored["resets_at"] = current_value


def _reset_window(
    stored: dict[str, object], raw_window: dict[str, object], used: float
) -> None:
    stored["used_percent"] = used
    stored["allocations"] = {}
    stored["calibration_samples"] = []
    stored["pending"] = {}
    reset = raw_window.get("resets_at")
    if isinstance(reset, str):
        stored["resets_at"] = reset
    else:
        stored.pop("resets_at", None)


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
                "cost": usage["cost"],
                "credits": usage["credits"],
            }
            if observed_at is not None:
                stored_usage["last_seen_at"] = observed_at
            current[key] = stored_usage
            earlier = previous_sessions.get(key)
            if isinstance(earlier, dict):
                old_cost = _number(earlier.get("cost")) or 0.0
                old_credits = _number(earlier.get("credits"))
                cost_delta = max(0.0, usage["cost"] - old_cost)
                credit_delta = (
                    max(0.0, usage["credits"] - old_credits)
                    if usage["credits"] is not None and old_credits is not None
                    else None
                )
                weight = _weight(
                    provider,
                    {"cost": cost_delta, "credits": credit_delta},
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
            window_key, old_window = _stored_window(windows_state, raw_window)
            used = _number(raw_window.get("used_percent"))
            if window_key is None or used is None:
                continue
            if not isinstance(old_window, dict):
                windows_state[window_key] = {
                    "used_percent": used,
                    "allocations": {},
                    "pending": {},
                }
                reset = raw_window.get("resets_at")
                if isinstance(reset, str):
                    windows_state[window_key]["resets_at"] = reset
                continue
            old_window.pop("rates", None)
            previous_used = _number(old_window.get("used_percent"))
            if previous_used is None:
                old_window["used_percent"] = used
                old_window["pending"] = {}
                _refresh_reset_timestamp(old_window, raw_window)
                continue
            has_current_reset = (
                _reset_timestamp(raw_window.get("resets_at")) is not None
            )
            if _reset_advanced(old_window, raw_window, observed_at) or (
                used < previous_used and not has_current_reset
            ):
                _reset_window(old_window, raw_window, used)
                continue
            _refresh_reset_timestamp(old_window, raw_window)
            pending = old_window.setdefault("pending", {})
            if not isinstance(pending, dict):
                old_window["pending"] = pending = {}
            for session_id, weight in interval_weights.items():
                pending[session_id] = (_number(pending.get(session_id)) or 0.0) + weight
            if used <= previous_used:
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
                _, stored = _stored_window(windows_state, raw_window)
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
                        # A share of a window cannot exceed the window's
                        # remaining headroom, whatever the calibrated rate says.
                        headroom_percent = max(0.0, 100.0 - used_percent)
                        estimate["projected_next_10_percent"] = round(
                            min(calibration * forecast_weight, headroom_percent), 2
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
