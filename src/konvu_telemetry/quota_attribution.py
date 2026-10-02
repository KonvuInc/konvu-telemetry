"""Estimate local-session shares of provider-reported subscription use."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import TypedDict

from .config import FORECAST_WINDOW
from .storage import quota_attribution_path, write_private_json


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
    for bucket in ("input", "output", "reasoning_output", "cache_write", "cache_read"):
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
    if session.get("cost_status") == "unavailable":
        return None
    cost = _number(session.get("total_cost_usd")) or 0.0
    credits = _number(session.get("total_credit_equivalent"))
    return {"cost": cost, "credits": credits, "tokens": _session_tokens(session)}


def _weight(provider: str, usage: SessionUsage) -> float:
    """What a session's work counts for when a rise is divided between sessions."""
    if usage["tokens"] > 0:
        return usage["tokens"]
    if provider == "codex" and usage["credits"] is not None:
        return usage["credits"]
    return usage["cost"]


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
                weight = _weight(
                    provider,
                    {
                        "cost": cost_delta,
                        "credits": credit_delta,
                        "tokens": max(0.0, usage["tokens"] - old_tokens),
                    },
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
                confirmed_share = (
                    _number(allocations.get(session["id"]))
                    if isinstance(allocations, dict)
                    else None
                )
                pending = stored.get("pending")
                # A session's share is its cut of the points the provider has
                # actually reported, and nothing else. Work done since the last
                # report has no measured cost yet, and pricing it from the
                # learned rate inflated the share by more than the point the
                # session was working towards.
                share = confirmed_share or 0.0
                prompts = _number(session.get("task_count"))
                projection: float | None = None
                if prompts is not None:
                    projection = _recent_burn(
                        stored, str(session["id"]), prompts, share
                    )
                    _record_share_history(stored, str(session["id"]), prompts, share)
                # A session with no reported points yet is shown as zero rather
                # than left blank. It is the truthful reading, and on a weekly
                # window the wait for a first point runs to hours.
                if (
                    share > 0
                    or session["id"] in (pending if isinstance(pending, dict) else {})
                    or projection is not None
                ):
                    estimate: dict[str, object] = {
                        "period": raw_window.get("period"),
                        "estimated_percent": round(share, 2),
                        "scope": "observed_window",
                    }
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
    """Count exhausted-plan session and subagent spend after the observed cutoff."""
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
            billing["subagent_offsets"] = {}
            continue
        previously_exhausted = billing.get("mode") == "exhausted"
        offsets_were_recorded = isinstance(billing.get("offsets"), dict)
        offsets = billing.setdefault("offsets", {})
        if not isinstance(offsets, dict):
            billing["offsets"] = offsets = {}
        subagent_offsets_were_recorded = isinstance(
            billing.get("subagent_offsets"), dict
        )
        subagent_offsets = billing.setdefault("subagent_offsets", {})
        if not isinstance(subagent_offsets, dict):
            billing["subagent_offsets"] = subagent_offsets = {}
        if not previously_exhausted or not offsets_were_recorded:
            offsets.clear()
            for session in rows:
                session_id = session.get("id")
                total = _number(session.get("total_cost_usd"))
                if isinstance(session_id, str) and total is not None:
                    offsets[session_id] = total
        if not previously_exhausted or not subagent_offsets_were_recorded:
            subagent_offsets.clear()
            for session in rows:
                session_id = session.get("id")
                agents = session.get("subagents")
                if not isinstance(session_id, str) or not isinstance(agents, list):
                    continue
                agent_offsets: dict[str, float] = {}
                for agent in agents:
                    if not isinstance(agent, dict):
                        continue
                    agent_id = agent.get("id")
                    agent_cost = _number(agent.get("cost_usd"))
                    if isinstance(agent_id, str) and agent_cost is not None:
                        agent_offsets[agent_id] = agent_cost
                subagent_offsets[session_id] = agent_offsets
        for session in rows:
            session_id = session.get("id")
            total = _number(session.get("total_cost_usd"))
            if not isinstance(session_id, str) or total is None:
                continue
            prior = _number(offsets.get(session_id)) or 0.0
            session["out_of_plan_spend_usd"] = round(max(0.0, total - prior), 6)
            session["out_of_plan_spend_status"] = "since_observed_plan_exit"
            agents = session.get("subagents")
            if not isinstance(agents, list):
                continue
            raw_agent_offsets = subagent_offsets.setdefault(session_id, {})
            agent_offsets = (
                raw_agent_offsets if isinstance(raw_agent_offsets, dict) else {}
            )
            if agent_offsets is not raw_agent_offsets:
                subagent_offsets[session_id] = agent_offsets
            out_of_plan_subagent_cost = 0.0
            for agent in agents:
                if not isinstance(agent, dict):
                    continue
                agent_id = agent.get("id")
                agent_cost = _number(agent.get("cost_usd"))
                if not isinstance(agent_id, str) or agent_cost is None:
                    continue
                agent_prior = _number(agent_offsets.get(agent_id)) or 0.0
                out_of_plan_cost = round(max(0.0, agent_cost - agent_prior), 6)
                agent["out_of_plan_cost_usd"] = out_of_plan_cost
                out_of_plan_subagent_cost += out_of_plan_cost
            session["out_of_plan_subagent_cost_usd"] = round(
                out_of_plan_subagent_cost, 6
            )
        billing["mode"] = "exhausted"
    write_private_json(quota_attribution_path(), state)
