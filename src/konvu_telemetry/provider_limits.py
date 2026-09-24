"""Fetch and normalize provider-reported account limits."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time


CODEX_LIMIT_TIMEOUT_SECONDS = 10.0


def _codex_executable() -> str | None:
    configured = shutil.which("codex")
    if configured is not None:
        return configured
    candidates = (
        Path.home() / ".local" / "bin" / "codex",
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    )
    return next(
        (
            str(candidate)
            for candidate in candidates
            if candidate.is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )


def _percentage(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    percentage = float(value)
    if not math.isfinite(percentage) or not 0 <= percentage <= 100:
        return None
    return percentage


def _reset_time(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def claude_limit_snapshot(
    payload: dict[str, object], session_id: str, captured_at: str
) -> dict[str, object] | None:
    """Normalize the documented Claude status-line rate-limit fields."""
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None
    windows: list[dict[str, object]] = []
    for source_name, period, minutes in (
        ("five_hour", "five_hour", 5 * 60),
        ("seven_day", "weekly", 7 * 24 * 60),
    ):
        raw_window = rate_limits.get(source_name)
        if not isinstance(raw_window, dict):
            continue
        used_percent = _percentage(raw_window.get("used_percentage"))
        if used_percent is None:
            continue
        windows.append(
            {
                "period": period,
                "window_minutes": minutes,
                "used_percent": used_percent,
                "remaining_percent": 100.0 - used_percent,
                "resets_at": _reset_time(raw_window.get("resets_at")),
                "session_id": session_id,
            }
        )
    return {
        "captured_at": captured_at,
        "source": "claude_statusline",
        "windows": windows,
    }


def codex_limit_snapshot(
    result: dict[str, object], captured_at: str
) -> dict[str, object] | None:
    """Normalize Codex's official account/rateLimits/read response."""
    rate_limits = result.get("rateLimits")
    if not isinstance(rate_limits, dict):
        return None
    windows: list[dict[str, object]] = []
    rolling = [rate_limits.get("primary"), rate_limits.get("secondary")]
    weekly = next(
        (
            window
            for window in rolling
            if isinstance(window, dict)
            and window.get("windowDurationMins") == 7 * 24 * 60
        ),
        None,
    )
    if isinstance(weekly, dict):
        used_percent = _percentage(weekly.get("usedPercent"))
        if used_percent is not None:
            windows.append(
                {
                    "period": "weekly",
                    "window_minutes": 7 * 24 * 60,
                    "used_percent": used_percent,
                    "remaining_percent": 100.0 - used_percent,
                    "resets_at": _reset_time(weekly.get("resetsAt")),
                }
            )
    monthly = rate_limits.get("individualLimit")
    if isinstance(monthly, dict):
        remaining_percent = _percentage(monthly.get("remainingPercent"))
        if remaining_percent is not None:
            window: dict[str, object] = {
                "period": "monthly",
                "used_percent": 100.0 - remaining_percent,
                "remaining_percent": remaining_percent,
                "resets_at": _reset_time(monthly.get("resetsAt")),
            }
            for name in ("used", "limit"):
                value = monthly.get(name)
                if isinstance(value, str):
                    window[name] = value
            windows.append(window)
    snapshot: dict[str, object] = {
        "captured_at": captured_at,
        "source": "codex_app_server",
        "windows": windows,
    }
    ordinary_usage_allowed = result.get("ordinaryUsageAllowed")
    if isinstance(ordinary_usage_allowed, bool):
        snapshot["ordinary_usage_allowed"] = ordinary_usage_allowed
    spend_control_reached = rate_limits.get("spendControlReached")
    if isinstance(spend_control_reached, bool):
        snapshot["spend_control_reached"] = spend_control_reached
    reached_type = rate_limits.get("rateLimitReachedType")
    if isinstance(reached_type, str) or reached_type is None:
        snapshot["rate_limit_reached_type"] = reached_type
    return snapshot


def _write_message(process: subprocess.Popen[str], message: dict[str, object]) -> None:
    if process.stdin is None:
        raise OSError("Codex app-server stdin is unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_response(
    process: subprocess.Popen[str], request_id: int, deadline: float
) -> dict[str, object] | None:
    if process.stdout is None:
        return None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            events = selector.select(deadline - time.monotonic())
            if not events:
                return None
            line = process.stdout.readline()
            if not line:
                return None
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            result = message.get("result")
            return result if isinstance(result, dict) else None
    return None


def fetch_codex_limits() -> dict[str, object] | None:
    """Fetch the current Codex account limits without blocking indefinitely."""
    executable = _codex_executable()
    if executable is None:
        return None
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        deadline = time.monotonic() + CODEX_LIMIT_TIMEOUT_SECONDS
        _write_message(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "konvu-telemetry", "version": "0.2"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        if _read_response(process, 1, deadline) is None:
            return None
        _write_message(process, {"method": "initialized"})
        _write_message(
            process,
            {
                "id": 2,
                "method": "account/rateLimits/read",
                "params": {"excludeResetCreditDetails": True},
            },
        )
        result = _read_response(process, 2, deadline)
        if result is None:
            return None
        captured = datetime.now(timezone.utc).isoformat()
        return codex_limit_snapshot(result, captured)
    except (OSError, ValueError):
        return None
    finally:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
