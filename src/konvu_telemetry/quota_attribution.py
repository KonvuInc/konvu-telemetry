"""Estimate local-session shares of provider-reported subscription use."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from threading import Lock
import time
from typing import TypedDict

from .config import FORECAST_WINDOW
from .pricing import load_pricing
from .storage import (
    analysis_usage_path,
    out_of_plan_spend_path,
    quota_attribution_path,
    write_private_json,
    write_private_json_if_changed,
)


class SessionUsage(TypedDict):
    cost: float
    credits: float | None
    tokens: float


class StoredSessionUsage(SessionUsage, total=False):
    last_seen_at: float


RESET_TIMESTAMP_JITTER_SECONDS = 60.0
LEGACY_RESET_MATCH_SECONDS = 2 * RESET_TIMESTAMP_JITTER_SECONDS
SESSION_BASELINE_RETENTION_SECONDS = 32 * 24 * 60 * 60
# 3: Session weight changed from raw token totals to recorded cost.
# 4: Quota-window identity stopped including mutable reset timestamps.
# Windows written before this release carry calibration samples instead of a
# share history. Both are ignored by the version that does not use them, and a
# window rebuilds whichever it needs from its next few ticks, so the shape
# changed without a version bump.
STATE_VERSION = 4
ANALYSIS_USAGE_VERSION = 1
ANALYSIS_USAGE_RETENTION_SECONDS = 8 * 24 * 60 * 60
_ANALYSIS_USAGE_LOCK = Lock()
_ANALYSIS_SESSION_PREFIX = "analysis:"


def record_analysis_usage(
    provider: str,
    parent_session_id: str,
    run_id: str,
    started_at: float,
    completed_at: float,
    tokens: int,
    *,
    model: str | None = None,
    usage_mode: str | None = None,
    token_usage: dict[str, int] | None = None,
    cost_usd: float | None = None,
    calls: int | None = None,
) -> None:
    """Queue one ephemeral analysis pass for normal quota attribution."""
    if (
        provider not in {"claude", "codex"}
        or not parent_session_id
        or not run_id
        or tokens <= 0
        or completed_at < started_at
    ):
        return
    with _ANALYSIS_USAGE_LOCK:
        try:
            raw = json.loads(analysis_usage_path().read_text())
        except (OSError, json.JSONDecodeError):
            raw = {"version": ANALYSIS_USAGE_VERSION, "events": {}}
        if not isinstance(raw, dict) or raw.get("version") != ANALYSIS_USAGE_VERSION:
            raw = {"version": ANALYSIS_USAGE_VERSION, "events": {}}
        events = raw.get("events")
        if not isinstance(events, dict):
            raw["events"] = events = {}
        # Retention follows the wall clock, never the incoming event, so one event with a
        # future timestamp cannot wipe the whole ledger.
        cutoff = min(completed_at, time.time()) - ANALYSIS_USAGE_RETENTION_SECONDS
        events = {
            event_id: event
            for event_id, event in events.items()
            if not isinstance(event, dict)
            or (_number(event.get("completed_at")) or completed_at) >= cutoff
        }
        raw["events"] = events
        details: dict[str, object] = {}
        if model:
            details["model"] = model
        if usage_mode:
            details["usage_mode"] = usage_mode
        if token_usage:
            details["token_usage"] = dict(token_usage)
        if cost_usd is not None and math.isfinite(cost_usd) and cost_usd >= 0:
            details["cost_usd"] = round(cost_usd, 6)
        # A pass makes one model call per batch; the hourly call cap reads this back.
        if calls is not None and calls > 0:
            details["calls"] = calls
        existing = events.get(run_id)
        if isinstance(existing, dict):
            # Runs recorded before pricing existed gain the fields they lack.
            missing = {
                key: value for key, value in details.items() if key not in existing
            }
            if not missing:
                return
            existing.update(missing)
        else:
            events[run_id] = {
                "provider": provider,
                "period": "five_hour" if provider == "claude" else "weekly",
                "parent_session_id": parent_session_id,
                "started_at": started_at,
                "completed_at": completed_at,
                "tokens": tokens,
                **details,
            }
        write_private_json(analysis_usage_path(), raw)


def _analysis_usage_events(
    reference_time: float | None = None,
) -> dict[str, dict[str, object]]:
    with _ANALYSIS_USAGE_LOCK:
        try:
            raw = json.loads(analysis_usage_path().read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        events = raw.get("events") if isinstance(raw, dict) else None
        if not isinstance(events, dict):
            return {}
        valid = {
            event_id: event
            for event_id, event in events.items()
            if isinstance(event_id, str) and isinstance(event, dict)
        }
        if reference_time is not None:
            cutoff = reference_time - ANALYSIS_USAGE_RETENTION_SECONDS
            valid = {
                event_id: event
                for event_id, event in valid.items()
                if (_number(event.get("completed_at")) or reference_time) >= cutoff
            }
            if len(valid) != len(events):
                raw["events"] = valid
                write_private_json(analysis_usage_path(), raw)
        return valid


def _analysis_log_covers(cutoff_at: float | None, observed_at: float | None) -> bool:
    """Check whether the retained run log still covers an older cutoff."""
    if (
        cutoff_at is None
        or observed_at is None
        or observed_at - cutoff_at > ANALYSIS_USAGE_RETENTION_SECONDS
    ):
        return False
    try:
        raw = json.loads(analysis_usage_path().read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(raw, dict)
        and raw.get("version") == ANALYSIS_USAGE_VERSION
        and isinstance(raw.get("events"), dict)
    )


ANALYSIS_TOKEN_BUCKETS = (
    "input",
    "output",
    "reasoning_output",
    "cache_write",
    "cache_read",
)


def _empty_analysis_usage() -> dict[str, object]:
    return {
        "run_count": 0,
        "included_run_count": 0,
        "spending_run_count": 0,
        "unknown_mode_run_count": 0,
        "tokens": {bucket: 0 for bucket in (*ANALYSIS_TOKEN_BUCKETS, "total")},
        "cost_usd": 0.0,
        "included_value_usd": 0.0,
        "spending_usd": 0.0,
        "unpriced_run_count": 0,
        "limit": [],
    }


def _add_analysis_run(totals: dict[str, object], event: dict[str, object]) -> None:
    """Fold one ledger run into its parent session's analysis totals."""
    mode = event.get("usage_mode")
    mode_key = (
        "included_run_count"
        if mode == "included"
        else "spending_run_count"
        if mode == "exhausted"
        else "unknown_mode_run_count"
    )
    for key in ("run_count", mode_key):
        totals[key] = int(_number(totals.get(key)) or 0) + 1
    tokens = totals["tokens"]
    assert isinstance(tokens, dict)
    tokens["total"] += int(_number(event.get("tokens")) or 0)
    breakdown = event.get("token_usage")
    if isinstance(breakdown, dict):
        for bucket in ANALYSIS_TOKEN_BUCKETS:
            tokens[bucket] += int(_number(breakdown.get(bucket)) or 0)
    cost = _number(event.get("cost_usd"))
    if cost is None:
        totals["unpriced_run_count"] = (
            int(_number(totals["unpriced_run_count"]) or 0) + 1
        )
        return
    totals["cost_usd"] = round((_number(totals["cost_usd"]) or 0.0) + cost, 6)
    # In-plan runs are not billed; their API-price value is kept apart from real spend.
    if mode == "included":
        totals["included_value_usd"] = round(
            (_number(totals["included_value_usd"]) or 0.0) + cost, 6
        )
    elif mode == "exhausted":
        totals["spending_usd"] = round(
            (_number(totals["spending_usd"]) or 0.0) + cost, 6
        )


def _analysis_session_id(parent_session_id: str) -> str:
    return _ANALYSIS_SESSION_PREFIX + parent_session_id


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


def _session_tokens(session: dict[str, object]) -> float:
    """Every token the provider metered for this session, cache reads included.

    Measured against real quota movement, a token costs about the same amount of
    limit whichever model spent it — within a fifth across the models observed —
    while the models' prices differ fivefold. Splitting a rise by price therefore
    over-credits whoever chose the dearer model.
    """
    usage = session.get("token_usage")
    if not isinstance(usage, dict):
        return 0.0
    total = 0.0
    for bucket in ("input", "output", "cache_write", "cache_read"):
        total += _number(usage.get(bucket)) or 0.0
    return total


def _session_usage(session: dict[str, object]) -> SessionUsage | None:
    """Weigh a session by the tokens it spent, falling back to its cost.

    Cost remains as a fallback for a session whose token counts have not been
    read yet, so a rise during that gap is still divided rather than dropped.
    """
    provider = session.get("provider")
    session_id = session.get("id")
    if provider not in {"claude", "codex"} or not isinstance(session_id, str):
        return None
    tokens = _session_tokens(session)
    # Shares follow tokens; a missing price only matters when there are no tokens either.
    if session.get("cost_status") == "unavailable" and tokens <= 0:
        return None
    cost = _number(session.get("total_cost_usd")) or 0.0
    credits = _number(session.get("total_credit_equivalent"))
    return {"cost": cost, "credits": credits, "tokens": tokens}


def _weight(provider: str, usage: SessionUsage) -> float:
    """What a session's work counts for when a rise is divided between sessions."""
    if usage["tokens"] > 0:
        return usage["tokens"]
    if provider == "codex" and usage["credits"] is not None:
        return usage["credits"]
    return usage["cost"]


def _snapshot_time(snapshot: dict[str, object]) -> float | None:
    return _timestamp(snapshot.get("generated_at"))


def _timestamp(value: object) -> float | None:
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


def _window_start(window: dict[str, object]) -> float | None:
    reset = _reset_timestamp(window.get("resets_at"))
    minutes = _number(window.get("window_minutes"))
    if minutes is None:
        period = window.get("period")
        minutes = {
            "five_hour": 300.0,
            "weekly": 10_080.0,
            "monthly": 43_200.0,
        }.get(period if isinstance(period, str) else "")
    return reset - minutes * 60 if reset is not None and minutes is not None else None


def _remove_analysis_rows(window: dict[str, object]) -> None:
    for field in ("allocations", "pending", "history"):
        rows = window.get(field)
        if not isinstance(rows, dict):
            continue
        for session_id in tuple(rows):
            if isinstance(session_id, str) and session_id.startswith(
                _ANALYSIS_SESSION_PREFIX
            ):
                rows.pop(session_id, None)


def _legacy_window_prefix(window: dict[str, object]) -> str | None:
    period = window.get("period")
    limit_id = window.get("limit_id", "default")
    if not isinstance(period, str) or not isinstance(limit_id, str):
        return None
    return f"{limit_id}:{period}:"


def _window_evidence(window: dict[str, object]) -> tuple[int, int, int]:
    allocations = window.get("allocations")
    pending = window.get("pending")
    history = window.get("history")
    return (
        len(history) if isinstance(history, dict) else 0,
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


def _reset_timestamp_advanced(
    stored: dict[str, object], raw_window: dict[str, object]
) -> bool:
    """Return whether the provider identifies a later quota window."""
    previous = _reset_timestamp(stored.get("resets_at"))
    current = _reset_timestamp(raw_window.get("resets_at"))
    return (
        previous is not None
        and current is not None
        and current - previous > RESET_TIMESTAMP_JITTER_SECONDS
    )


def _reset_advanced(
    stored: dict[str, object],
    raw_window: dict[str, object],
    observed_at: float | None,
) -> bool:
    """Return whether the previous quota window has reached its deadline."""
    previous = _reset_timestamp(stored.get("resets_at"))
    return (
        _reset_timestamp_advanced(stored, raw_window)
        and previous is not None
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


def _record_share_history(
    window: dict[str, object], session_id: str, prompts: float, share: float
) -> None:
    """Keep enough of a session's share history to look back ten prompts.

    One entry per prompt count, so a session polled every two minutes between
    prompts does not fill the list with copies of the same reading.
    """
    history = window.setdefault("history", {})
    if not isinstance(history, dict):
        window["history"] = history = {}
    entries = history.setdefault(session_id, [])
    if not isinstance(entries, list):
        history[session_id] = entries = []
    last = entries[-1] if entries else None
    if isinstance(last, list) and len(last) == 2 and _number(last[0]) == prompts:
        entries[-1] = [prompts, share]
    else:
        entries.append([prompts, share])
    history[session_id] = entries[-(2 * FORECAST_WINDOW + 2) :]


def _recent_burn(
    window: dict[str, object], session_id: str, prompts: float, share: float
) -> float | None:
    """The quota this session was credited over its last ten prompts.

    Used as the projection for its next ten. Backtested against what Codex
    reported next over 2,565 real ten-prompt stretches, this beat converting a
    projected spend through a learned rate, and it needs no conversion at all.
    """
    history = window.get("history")
    entries = history.get(session_id) if isinstance(history, dict) else None
    if not isinstance(entries, list) or not entries:
        return None
    earlier = [
        entry
        for entry in entries
        if isinstance(entry, list)
        and len(entry) == 2
        and (_number(entry[0]) or 0.0) <= prompts - FORECAST_WINDOW
    ]
    if earlier:
        return max(0.0, share - (_number(earlier[-1][1]) or 0.0))
    # Fewer than ten prompts recorded: scale what the session has burned so far.
    first = entries[0]
    if not (isinstance(first, list) and len(first) == 2):
        return None
    span = prompts - (_number(first[0]) or 0.0)
    if span <= 0:
        return None
    return max(0.0, (share - (_number(first[1]) or 0.0)) / span * FORECAST_WINDOW)


def _spread_over_existing(allocations: dict[str, object], increase: float) -> None:
    """Share a rise nobody was recorded for across whoever this window already credits.

    Leaves it unattributed only when the window credits no one at all, because
    there is then nothing to go on.
    """
    shares = {
        session_id: value
        for session_id, raw in allocations.items()
        if isinstance(session_id, str) and (value := _number(raw)) and value > 0
    }
    total = sum(shares.values())
    if total <= 0:
        return
    for session_id, value in shares.items():
        allocations[session_id] = value + increase * value / total


def _reset_window(
    stored: dict[str, object], raw_window: dict[str, object], used: float
) -> None:
    stored["used_percent"] = used
    stored["allocations"] = {}
    stored["history"] = {}
    stored["pending"] = {}
    reset = raw_window.get("resets_at")
    if isinstance(reset, str):
        stored["resets_at"] = reset
    else:
        stored.pop("resets_at", None)


def _observation_reason(
    provider: str,
    session_id: str,
    account: dict[str, object],
    raw_windows: list[object],
    windows_state: dict[str, object],
    new_window_keys: set[str],
    reset_window_keys: set[str],
) -> str:
    if account.get("status") == "unavailable":
        return "provider_unavailable"
    primary_period = "weekly" if provider == "codex" else "five_hour"
    primary = next(
        (
            window
            for window in raw_windows
            if isinstance(window, dict) and window.get("period") == primary_period
        ),
        None,
    )
    if not isinstance(primary, dict):
        return "provider_unavailable"
    primary_key = _window_key(primary)
    used = _number(primary.get("used_percent"))
    if used == 0 or primary_key in reset_window_keys:
        return "window_reset"
    if primary_key in new_window_keys:
        return "establishing_baseline"
    _, stored = _stored_window(windows_state, primary)
    if not isinstance(stored, dict):
        return "establishing_baseline"
    pending = stored.get("pending")
    if isinstance(pending, dict) and (_number(pending.get(session_id)) or 0) > 0:
        return "waiting_for_quota_change"
    return "waiting_for_activity"


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
    applied_events = state.setdefault("analysis_events_applied", {})
    if not isinstance(applied_events, dict):
        state["analysis_events_applied"] = applied_events = {}
    retention_time = observed_at
    if retention_time is None:
        retention_time = max(
            (
                timestamp
                for account in quotas.values()
                if isinstance(account, dict)
                for timestamp in [_timestamp(account.get("observed_at"))]
                if timestamp is not None
            ),
            default=None,
        )
    analysis_events = _analysis_usage_events(retention_time)
    retained_event_ids = set(analysis_events)
    applied_events = {
        event_id: value
        for event_id, value in applied_events.items()
        if event_id in retained_event_ids
    }
    state["analysis_events_applied"] = applied_events
    analysis_run_counts: dict[tuple[str, str], int] = {}
    analysis_totals: dict[tuple[str, str], dict[str, object]] = {}
    for event in analysis_events.values():
        provider = event.get("provider")
        parent_id = event.get("parent_session_id")
        if (
            not isinstance(provider, str)
            or provider not in {"claude", "codex"}
            or not isinstance(parent_id, str)
            or (_number(event.get("tokens")) or 0) <= 0
            or _number(event.get("completed_at")) is None
        ):
            continue
        analysis_key = (provider, parent_id)
        analysis_run_counts[analysis_key] = analysis_run_counts.get(analysis_key, 0) + 1
        _add_analysis_run(
            analysis_totals.setdefault(analysis_key, _empty_analysis_usage()), event
        )
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
        new_window_keys: set[str] = set()
        reset_window_keys: set[str] = set()
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
                "tokens": usage["tokens"],
            }
            if observed_at is not None:
                stored_usage["last_seen_at"] = observed_at
            current[key] = stored_usage
            earlier = previous_sessions.get(key)
            if isinstance(earlier, dict):
                old_cost = _number(earlier.get("cost")) or 0.0
                old_credits = _number(earlier.get("credits"))
                old_tokens = _number(earlier.get("tokens")) or 0.0
                cost_delta = max(0.0, usage["cost"] - old_cost)
                credit_delta = (
                    max(0.0, usage["credits"] - old_credits)
                    if usage["credits"] is not None and old_credits is not None
                    else None
                )
                token_delta = max(0.0, usage["tokens"] - old_tokens)
                # Once a session reports tokens, only new tokens earn a share: a price that
                # arrives late must not pin a quota rise on a session that did no new work.
                weight = (
                    token_delta
                    if usage["tokens"] > 0 and old_tokens > 0
                    else _weight(
                        provider,
                        {
                            "cost": cost_delta,
                            "credits": credit_delta,
                            "tokens": token_delta,
                        },
                    )
                )
                if weight > 0:
                    interval_weights[key] = weight
        quota_observed_at = _timestamp(account.get("observed_at"))
        primary_period = "five_hour" if provider == "claude" else "weekly"
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
            period = raw_window.get("period")
            if isinstance(old_window, dict) and period != primary_period:
                _remove_analysis_rows(old_window)
            window_start = _window_start(raw_window)
            for event_id, event in analysis_events.items():
                event_period = event.get("period", primary_period)
                completed_at = _number(event.get("completed_at"))
                if (
                    event_id not in applied_events
                    and event.get("provider") == provider
                    and event_period == period
                    and completed_at is not None
                    and window_start is not None
                    and completed_at < window_start
                ):
                    applied_events[event_id] = completed_at
            if not isinstance(old_window, dict):
                new_window_keys.add(window_key)
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
                used < previous_used
                and (
                    not has_current_reset
                    or _reset_timestamp_advanced(old_window, raw_window)
                )
            ):
                _reset_window(old_window, raw_window, used)
                reset_window_keys.add(window_key)
                continue
            _refresh_reset_timestamp(old_window, raw_window)
            pending = old_window.setdefault("pending", {})
            if not isinstance(pending, dict):
                old_window["pending"] = pending = {}
            for session_id, weight in interval_weights.items():
                pending[session_id] = (_number(pending.get(session_id)) or 0.0) + weight
            if period == primary_period and quota_observed_at is not None:
                for event_id, event in analysis_events.items():
                    if (
                        event_id in applied_events
                        or event.get("provider") != provider
                        or event.get("period", primary_period) != period
                    ):
                        continue
                    parent_session_id = event.get("parent_session_id")
                    completed_at = _number(event.get("completed_at"))
                    tokens = _number(event.get("tokens"))
                    if (
                        not isinstance(parent_session_id, str)
                        or completed_at is None
                        or tokens is None
                        or tokens <= 0
                        or completed_at > quota_observed_at
                        or (window_start is not None and completed_at < window_start)
                    ):
                        continue
                    analysis_session_id = _analysis_session_id(parent_session_id)
                    pending[analysis_session_id] = (
                        _number(pending.get(analysis_session_id)) or 0.0
                    ) + tokens
                    applied_events[event_id] = completed_at
            if used <= previous_used:
                continue
            increase = used - previous_used
            allocations = old_window.setdefault("allocations", {})
            if not isinstance(allocations, dict):
                old_window["allocations"] = allocations = {}
            total_weight = sum(_number(weight) or 0.0 for weight in pending.values())
            if total_weight > 0:
                for session_id, raw_weight in pending.items():
                    weight = _number(raw_weight) or 0.0
                    allocations[session_id] = (
                        _number(allocations.get(session_id)) or 0.0
                    ) + increase * weight / total_weight
            else:
                # One burst of work can raise the reported percentage twice. The
                # first rise empties the tally, so the second arrives with nothing
                # recorded against it. Dropping it loses the point for good, and
                # on this machine that was 9 of 20 rises. The sessions already
                # credited in this window are the best account of who did it.
                _spread_over_existing(allocations, increase)
                # No weight was measured, so this rise teaches nothing about rates.
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
                own_share = (
                    _number(allocations.get(session["id"]))
                    if isinstance(allocations, dict)
                    else None
                )
                analysis_session_id = _analysis_session_id(str(session["id"]))
                analysis_share = (
                    _number(allocations.get(analysis_session_id))
                    if isinstance(allocations, dict)
                    else None
                )
                pending = stored.get("pending")
                # A session's share is its cut of the points the provider has
                # actually reported, and nothing else. Work done since the last
                # report has no measured cost yet, and pricing it from the
                # learned rate inflated the share by more than the point the
                # session was working towards.
                session_share = own_share or 0.0
                analysis_share = analysis_share or 0.0
                share = session_share + analysis_share
                pending_rows = pending if isinstance(pending, dict) else {}
                session_pending = session["id"] in pending_rows
                analysis_pending = analysis_session_id in pending_rows
                prompts = _number(session.get("task_count"))
                projection: float | None = None
                if prompts is not None:
                    projection = _recent_burn(
                        stored, str(session["id"]), prompts, session_share
                    )
                    _record_share_history(
                        stored, str(session["id"]), prompts, session_share
                    )
                # A session with no reported points yet is shown as zero rather
                # than left blank. It is the truthful reading, and on a weekly
                # window the wait for a first point runs to hours.
                if (
                    share > 0
                    or session_pending
                    or analysis_pending
                    or projection is not None
                ):
                    estimate: dict[str, object] = {
                        "period": raw_window.get("period"),
                        "estimated_percent": round(share, 2),
                        "scope": "observed_window",
                    }
                    if analysis_share > 0:
                        estimate["analysis_estimated_percent"] = round(
                            analysis_share, 2
                        )
                    if analysis_pending:
                        estimate["analysis_pending"] = True
                        pending_total = sum(
                            _number(weight) or 0.0 for weight in pending_rows.values()
                        )
                        if pending_total > 0:
                            # Share of the work done since the last reported point.
                            estimate["analysis_unreported_work_share"] = round(
                                (_number(pending_rows.get(analysis_session_id)) or 0.0)
                                / pending_total,
                                4,
                            )
                    if projection is not None:
                        # A share of a window cannot exceed what is left of it,
                        # however fast the last ten prompts were going.
                        headroom_percent = max(0.0, 100.0 - used_percent)
                        estimate["projected_next_10_percent"] = round(
                            min(projection, headroom_percent), 2
                        )
                    estimates.append(estimate)
            session["quota_attribution"] = {
                "state": "observing" if not estimates else "estimated",
                "windows": estimates,
            }
            run_count = analysis_run_counts.get((provider, str(session["id"])), 0)
            if run_count:
                session["quota_attribution"]["analysis_run_count"] = run_count
            totals = analysis_totals.get((provider, str(session["id"])))
            if totals is not None:
                totals["limit"] = [
                    {
                        "period": estimate.get("period"),
                        "measured_percent": estimate.get(
                            "analysis_estimated_percent", 0.0
                        ),
                        # The window has not moved a full point since these runs.
                        "unreported": {
                            "below_percent": 1.0,
                            "share_of_unreported_work": estimate.get(
                                "analysis_unreported_work_share"
                            ),
                        }
                        if estimate.get("analysis_pending")
                        else None,
                    }
                    for estimate in estimates
                    if estimate.get("analysis_estimated_percent")
                    or estimate.get("analysis_pending")
                ]
                session["analysis_usage"] = totals
            if not estimates:
                session["quota_attribution"]["reason"] = _observation_reason(
                    provider,
                    str(session["id"]),
                    account,
                    raw_windows,
                    windows_state,
                    new_window_keys,
                    reset_window_keys,
                )
    # Analysis spend is recorded even when no quota windows could be read for its provider.
    for session in session_rows:
        session_key = (str(session.get("provider")), str(session.get("id")))
        if "analysis_usage" not in session and session_key in analysis_totals:
            session["analysis_usage"] = {**analysis_totals[session_key], "limit": []}
    write_private_json(quota_attribution_path(), state)


def _account_exhausted(account: object) -> bool | None:
    if not isinstance(account, dict):
        return None
    status = account.get("status")
    if isinstance(status, str) and status in {"stale", "fetching", "unavailable"}:
        return None
    if account.get("ordinary_usage_allowed") is False:
        return True
    windows = account.get("windows")
    ordinary = [
        _number(window.get("used_percent"))
        for window in (windows if isinstance(windows, list) else [])
        if isinstance(window, dict) and window.get("period") in {"five_hour", "weekly"}
    ]
    if any(used is not None and used >= 100 for used in ordinary):
        return True
    if account.get("ordinary_usage_allowed") is True:
        return False
    return False if ordinary and all(used is not None for used in ordinary) else None


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
        exhausted = _account_exhausted(account)
        session["usage_mode"] = (
            "exhausted"
            if exhausted
            else "included"
            if exhausted is False
            else "unknown"
        )


def _late_session_offset(session: dict[str, object], cutoff_at: float | None) -> float:
    """Use priced iterations to separate a newly seen session's earlier work."""
    total = _number(session.get("total_cost_usd")) or 0.0
    iterations = session.get("iterations")
    if cutoff_at is None or not isinstance(iterations, list) or not iterations:
        return total
    historical = 0.0
    all_cost = 0.0
    for row in iterations:
        if not isinstance(row, dict):
            return total
        started_at = _timestamp(row.get("started_at"))
        if started_at is None:
            return total
        if row.get("priced") is False and row.get("cost_usd") is None:
            continue
        cost = _number(row.get("cost_usd"))
        if row.get("priced") is not True or cost is None:
            return total
        all_cost += cost
        if started_at < cutoff_at:
            historical += cost
    return round(historical, 6) if abs(all_cost - total) <= 0.00001 else total


def _event_spend_since_cutoff(
    session: dict[str, object],
    cutoff_at: float | None,
    total: float,
    excluded: list[tuple[float, float]],
) -> tuple[float, dict[str, float] | None] | None:
    """Price observed events by their own timestamps, including active iterations."""
    events = session.get("_billing_events")
    if cutoff_at is None or not isinstance(events, list):
        return None
    all_cost = 0.0
    paid_cost = 0.0
    agent_costs: dict[str, float] | None = {}
    for event in events:
        if not isinstance(event, dict):
            return None
        timestamp = _number(event.get("timestamp"))
        cost = _number(event.get("cost_usd"))
        if timestamp is None or cost is None:
            return None
        all_cost += cost
        if timestamp >= cutoff_at and not any(
            start <= timestamp < end for start, end in excluded
        ):
            paid_cost += cost
            agent_id = event.get("agent_id")
            if event.get("is_subagent") is True and not isinstance(agent_id, str):
                agent_costs = None
            elif isinstance(agent_id, str) and agent_costs is not None:
                agent_costs[agent_id] = round(agent_costs.get(agent_id, 0.0) + cost, 6)
    return (
        (round(paid_cost, 6), agent_costs) if abs(all_cost - total) <= 0.00001 else None
    )


def _spend_complete(
    spend: dict[str, object],
    complete: dict[str, object],
    analysis_runs: dict[str, object],
    analysis_complete: bool,
    history_complete: bool,
) -> bool:
    return (
        (bool(spend) or bool(analysis_runs))
        and analysis_complete
        and history_complete
        and all(value is True for value in complete.values())
        and all(
            session_id in complete and _number(value) is not None
            for session_id, value in spend.items()
        )
    )


def _analysis_spend(runs: dict[str, object], fingerprint: str) -> tuple[float, bool]:
    """Sum deduplicated paid analysis runs, including runs past log retention."""
    total = 0.0
    complete = True
    for run in runs.values():
        if not isinstance(run, dict):
            complete = False
            continue
        mode = run.get("usage_mode")
        cost = _number(run.get("cost_usd"))
        if mode == "exhausted" and cost is not None:
            total += cost
            if run.get("pricing") != fingerprint:
                complete = False
        else:
            complete = False
    return round(total, 6), complete


def _record_analysis_spend(
    runs: dict[str, object],
    events: dict[str, dict[str, object]],
    provider: str,
    cutoff_at: float | None,
    excluded: list[tuple[float, float]],
    fingerprint: str,
) -> None:
    """Keep one billing record per eligible run until the quota episode resets."""
    for run_id, event in events.items():
        if event.get("provider") != provider:
            continue
        completed_at = _number(event.get("completed_at"))
        if completed_at is None or cutoff_at is None or completed_at < cutoff_at:
            continue
        if any(start <= completed_at < end for start, end in excluded):
            continue
        started_at = _number(event.get("started_at"))
        if started_at is None or started_at < cutoff_at:
            runs[run_id] = {
                "usage_mode": "unknown",
                "cost_usd": _number(event.get("cost_usd")),
                "pricing": fingerprint,
            }
            continue
        mode = event.get("usage_mode")
        if mode == "included":
            runs.pop(run_id, None)
            continue
        cost = _number(event.get("cost_usd"))
        previous = runs.get(run_id)
        if not isinstance(previous, dict):
            runs[run_id] = {
                "usage_mode": mode,
                "cost_usd": cost,
                "pricing": fingerprint,
            }
            continue
        if mode == "exhausted":
            previous["usage_mode"] = mode
        if cost is not None and _number(previous.get("cost_usd")) != cost:
            previous["cost_usd"] = cost
            previous["pricing"] = fingerprint


def _excluded_intervals(billing: dict[str, object]) -> list[tuple[float, float]]:
    """Return intervals where quota state could have reset without observation."""
    raw_intervals = billing.get("uncertain_intervals")
    excluded: list[tuple[float, float]] = []
    if raw_intervals is None:
        return excluded
    if not isinstance(raw_intervals, list):
        billing["history_uncertain"] = True
        return excluded
    for interval in raw_intervals:
        if isinstance(interval, list) and len(interval) == 2:
            start, end = (_number(value) for value in interval)
            if start is not None and end is not None and end >= start:
                excluded.append((start, end))
                continue
        billing["history_uncertain"] = True
    return excluded


def apply_out_of_plan_accounting(
    snapshot: dict[str, object], prices: dict[str, dict[str, float]] | None = None
) -> None:
    """Count exhausted-plan session and subagent spend after the observed cutoff."""
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list):
        return
    state = _load_state()
    providers = state["providers"]
    assert isinstance(providers, dict)
    summary_providers: dict[str, dict[str, object]] = {}
    summary: dict[str, object] = {"version": 1, "providers": summary_providers}
    quotas = snapshot.get("account_quotas")
    observed_at = _snapshot_time(snapshot)
    analysis_events = _analysis_usage_events(observed_at)
    pricing_fingerprint = hashlib.sha256(
        json.dumps(
            prices if prices is not None else load_pricing(), sort_keys=True
        ).encode()
    ).hexdigest()
    for provider in ("claude", "codex"):
        rows = [
            session
            for session in sessions
            if isinstance(session, dict) and session.get("provider") == provider
        ]
        provider_state = providers.setdefault(provider, {"sessions": {}, "windows": {}})
        if not isinstance(provider_state, dict):
            providers[provider] = provider_state = {"sessions": {}, "windows": {}}
        billing = provider_state.setdefault("billing", {})
        if not isinstance(billing, dict):
            provider_state["billing"] = billing = {}
        billing.pop("subagent_offsets", None)
        prior_analysis_runs = billing.get("analysis_runs")
        if not isinstance(prior_analysis_runs, dict):
            billing["analysis_runs"] = analysis_runs = {}
            if billing.get("mode") == "exhausted" and not _analysis_log_covers(
                _number(billing.get("cutoff_at")), observed_at
            ):
                billing["history_uncertain"] = True
        else:
            analysis_runs = prior_analysis_runs
        spend = billing.get("session_spend")
        if not isinstance(spend, dict):
            spend = {}
            previous_sessions = provider_state.get("sessions")
            offsets = billing.get("offsets")
            if isinstance(previous_sessions, dict) and isinstance(offsets, dict):
                for session_id, usage in previous_sessions.items():
                    if not isinstance(usage, dict):
                        continue
                    cost = _number(usage.get("cost"))
                    offset = _number(offsets.get(session_id))
                    if cost is not None and offset is not None:
                        spend[session_id] = round(max(0.0, cost - offset), 6)
            billing["session_spend"] = spend
        complete = billing.get("session_complete")
        if not isinstance(complete, dict):
            billing["session_complete"] = complete = {}
        raw_pricing = billing.setdefault("session_pricing", {})
        session_pricing = raw_pricing if isinstance(raw_pricing, dict) else {}
        if session_pricing is not raw_pricing:
            billing["session_pricing"] = session_pricing
        for session_id in spend:
            if session_pricing.get(session_id) != pricing_fingerprint:
                complete[session_id] = False
        account = quotas.get(provider) if isinstance(quotas, dict) else None
        exhaustion = _account_exhausted(account)
        if not isinstance(account, dict) and rows:
            modes = {session.get("usage_mode") for session in rows}
            exhaustion = (
                True
                if modes == {"exhausted"}
                else False
                if modes == {"included"}
                else None
            )
        if exhaustion is None:
            mode = billing.get("mode")
            analysis_total, _ = _analysis_spend(analysis_runs, pricing_fingerprint)
            if mode == "exhausted":
                billing["history_uncertain"] = True
                if "uncertain_since" not in billing:
                    billing["uncertain_since"] = (
                        _number(billing.get("last_fresh_at"))
                        or _number(billing.get("cutoff_at"))
                        or observed_at
                    )
            summary_providers[provider] = {
                "status": mode if mode in {"included", "exhausted"} else "unknown",
                "spend_usd": round(
                    sum(_number(value) or 0.0 for value in spend.values())
                    + analysis_total,
                    6,
                )
                if mode == "exhausted"
                else 0.0,
                "complete": False,
            }
            cutoff_at = _number(billing.get("cutoff_at"))
            if mode == "exhausted" and cutoff_at is not None:
                summary_providers[provider]["cutoff_at"] = cutoff_at
            continue
        if not exhaustion:
            billing["mode"] = "included"
            billing["offsets"] = {}
            billing["unpriced_baselines"] = {}
            billing["session_spend"] = {}
            billing["session_complete"] = {}
            billing["session_pricing"] = {}
            billing["analysis_runs"] = {}
            billing.pop("analysis_last_scan_at", None)
            billing.pop("history_uncertain", None)
            billing.pop("uncertain_since", None)
            billing.pop("uncertain_intervals", None)
            billing.pop("last_fresh_at", None)
            billing.pop("cutoff_at", None)
            summary_providers[provider] = {
                "status": "included",
                "spend_usd": 0.0,
                "complete": True,
            }
            continue
        previously_exhausted = billing.get("mode") == "exhausted"
        offsets_were_recorded = isinstance(billing.get("offsets"), dict)
        offsets = billing.setdefault("offsets", {})
        if not isinstance(offsets, dict):
            billing["offsets"] = offsets = {}
        raw_baselines = billing.setdefault("unpriced_baselines", {})
        baselines = raw_baselines if isinstance(raw_baselines, dict) else {}
        if baselines is not raw_baselines:
            billing["unpriced_baselines"] = baselines
        if (
            previously_exhausted
            and billing.get("uncertain_since") is not None
            and observed_at is None
        ):
            analysis_total, _ = _analysis_spend(analysis_runs, pricing_fingerprint)
            summary_providers[provider] = {
                "status": "exhausted",
                "spend_usd": round(
                    sum(_number(value) or 0.0 for value in spend.values())
                    + analysis_total,
                    6,
                ),
                "complete": False,
            }
            cutoff_at = _number(billing.get("cutoff_at"))
            if cutoff_at is not None:
                summary_providers[provider]["cutoff_at"] = cutoff_at
            continue
        if not previously_exhausted or not offsets_were_recorded:
            offsets.clear()
            baselines.clear()
            spend.clear()
            complete.clear()
            session_pricing.clear()
            analysis_runs.clear()
            billing.pop("history_uncertain", None)
            billing.pop("uncertain_since", None)
            billing.pop("uncertain_intervals", None)
            billing["analysis_last_scan_at"] = observed_at
            cutoff_at = observed_at
            if cutoff_at is None:
                billing.pop("cutoff_at", None)
            else:
                billing["cutoff_at"] = cutoff_at
            for session in rows:
                session_id = session.get("id")
                total = _number(session.get("total_cost_usd"))
                if isinstance(session_id, str) and total is not None:
                    offsets[session_id] = total
                    iterations = session.get("iterations")
                    baselines[session_id] = [
                        row["started_at"]
                        for row in (iterations if isinstance(iterations, list) else [])
                        if isinstance(row, dict)
                        and row.get("priced") is False
                        and isinstance(row.get("started_at"), str)
                    ]
        else:
            uncertain_since = _number(billing.pop("uncertain_since", None))
            if uncertain_since is not None and observed_at is not None:
                intervals = billing.setdefault("uncertain_intervals", [])
                if not isinstance(intervals, list):
                    billing["uncertain_intervals"] = intervals = []
                if observed_at > uncertain_since:
                    intervals.append([uncertain_since, observed_at])
            last_scan = _number(billing.get("analysis_last_scan_at"))
            if (
                observed_at is not None
                and last_scan is not None
                and observed_at - last_scan > ANALYSIS_USAGE_RETENTION_SECONDS
            ):
                billing["history_uncertain"] = True
        excluded = _excluded_intervals(billing)
        for session in rows:
            session_id = session.get("id")
            total = _number(session.get("total_cost_usd"))
            if not isinstance(session_id, str):
                continue
            if total is None:
                complete[session_id] = False
                continue
            if session_id not in offsets:
                cutoff_at = _number(billing.get("cutoff_at"))
                offsets[session_id] = _late_session_offset(session, cutoff_at)
                iterations = session.get("iterations")
                baselines[session_id] = [
                    row["started_at"]
                    for row in (iterations if isinstance(iterations, list) else [])
                    if isinstance(row, dict)
                    and row.get("priced") is False
                    and isinstance(row.get("started_at"), str)
                    and (
                        cutoff_at is None
                        or (started_at := _timestamp(row["started_at"])) is None
                        or started_at < cutoff_at
                    )
                ]
            iterations = session.get("iterations")
            pending = baselines.get(session_id)
            if isinstance(pending, list) and isinstance(iterations, list):
                pending_ids = {value for value in pending if isinstance(value, str)}
                for row in iterations:
                    if (
                        not isinstance(row, dict)
                        or row.get("started_at") not in pending_ids
                    ):
                        continue
                    cost = _number(row.get("cost_usd"))
                    if row.get("priced") is True and cost is not None:
                        offsets[session_id] = round(
                            (_number(offsets.get(session_id)) or 0.0) + cost, 6
                        )
                        pending_ids.remove(row["started_at"])
                baselines[session_id] = sorted(pending_ids)
            observed = _event_spend_since_cutoff(
                session, _number(billing.get("cutoff_at")), total, excluded
            )
            baseline_complete = (
                observed is not None and billing.get("history_uncertain") is not True
            )
            complete[session_id] = (
                session.get("cost_status") == "complete" and baseline_complete
            )
            session_pricing[session_id] = pricing_fingerprint
            prior = _number(offsets.get(session_id)) or 0.0
            session["out_of_plan_spend_usd"] = (
                observed[0]
                if observed is not None
                else round(max(0.0, total - prior), 6)
            )
            session["out_of_plan_spend_status"] = (
                "since_observed_plan_exit"
                if baseline_complete
                else "incomplete_baseline"
            )
            spend[session_id] = session["out_of_plan_spend_usd"]
            agents = session.get("subagents")
            if not isinstance(agents, list):
                continue
            agent_spend = observed[1] if observed is not None else None
            if agent_spend is None:
                session.pop("out_of_plan_subagent_cost_usd", None)
                for agent in agents:
                    if isinstance(agent, dict):
                        agent.pop("out_of_plan_cost_usd", None)
                continue
            out_of_plan_subagent_cost = 0.0
            for agent in agents:
                if not isinstance(agent, dict):
                    continue
                agent_id = agent.get("id")
                if not isinstance(agent_id, str):
                    continue
                out_of_plan_cost = agent_spend.get(agent_id, 0.0)
                agent["out_of_plan_cost_usd"] = out_of_plan_cost
                out_of_plan_subagent_cost += out_of_plan_cost
            session["out_of_plan_subagent_cost_usd"] = round(
                out_of_plan_subagent_cost, 6
            )
        billing["mode"] = "exhausted"
        billing["last_fresh_at"] = observed_at
        billing["analysis_last_scan_at"] = observed_at
        _record_analysis_spend(
            analysis_runs,
            analysis_events,
            provider,
            _number(billing.get("cutoff_at")),
            excluded,
            pricing_fingerprint,
        )
        analysis_total, analysis_complete = _analysis_spend(
            analysis_runs, pricing_fingerprint
        )
        summary_providers[provider] = {
            "status": "exhausted",
            "spend_usd": round(
                sum(_number(value) or 0.0 for value in spend.values()) + analysis_total,
                6,
            ),
            "complete": _spend_complete(
                spend,
                complete,
                analysis_runs,
                analysis_complete,
                billing.get("history_uncertain") is not True,
            ),
        }
        cutoff_at = _number(billing.get("cutoff_at"))
        if cutoff_at is not None:
            summary_providers[provider]["cutoff_at"] = cutoff_at
    write_private_json(quota_attribution_path(), state)
    write_private_json_if_changed(out_of_plan_spend_path(), summary)
