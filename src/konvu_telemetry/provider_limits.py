"""Read provider-reported account limits without retaining credentials."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import stat
import subprocess
import sys
import time
from typing import Callable, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


CLAUDE_USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
POLL_INTERVAL_SECONDS = 120.0
RESULT_GRACE_SECONDS = 10 * 60.0
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 256 * 1024

FailureReason = Literal[
    "authentication_failed",
    "credentials_unavailable",
    "invalid_response",
    "network_error",
    "provider_error",
    "rate_limited",
    "service_unavailable",
]


class HTTPResponse(Protocol):
    headers: object

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> HTTPResponse: ...

    def __exit__(self, *args: object) -> None: ...


OpenURL = Callable[[Request, float], HTTPResponse]


@dataclass(frozen=True)
class FetchResult:
    snapshot: dict[str, object] | None
    retry_after_seconds: float | None = None
    unavailable: bool = False
    failure: FailureReason | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _iso_now(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def _percentage(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    percentage = float(value)
    return percentage if math.isfinite(percentage) and 0 <= percentage <= 100 else None


def _iso_reset(value: object) -> str | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _period(minutes: float) -> str:
    if 299 <= minutes <= 301:
        return "five_hour"
    if 10_079 <= minutes <= 10_081:
        return "weekly"
    return "custom"


def _window(
    period: str,
    used_percent: float,
    resets_at: str | None,
    window_minutes: float | None = None,
    limit_id: str = "default",
) -> dict[str, object]:
    result: dict[str, object] = {
        "limit_id": limit_id,
        "period": period,
        "used_percent": used_percent,
        "remaining_percent": 100.0 - used_percent,
        "resets_at": resets_at,
    }
    if window_minutes is not None:
        result["window_minutes"] = window_minutes
    return result


def decode_claude_usage(body: object, captured_at: str) -> dict[str, object] | None:
    """Normalize Claude's live five-hour and weekly account windows."""
    if not isinstance(body, dict):
        return None
    windows: list[dict[str, object]] = []
    for source, period, minutes in (
        ("five_hour", "five_hour", 300.0),
        ("seven_day", "weekly", 10_080.0),
    ):
        raw = body.get(source)
        if not isinstance(raw, dict):
            continue
        used = _percentage(raw.get("utilization"))
        if used is not None:
            windows.append(
                _window(period, used, _iso_reset(raw.get("resets_at")), minutes)
            )
    if not windows:
        return None
    return {"observed_at": captured_at, "source": "provider_api", "windows": windows}


def decode_codex_usage(body: object, captured_at: str) -> dict[str, object] | None:
    """Normalize every live Codex rolling window and optional spend limit."""
    if not isinstance(body, dict):
        return None
    default_limits = body.get("rateLimits")
    if not isinstance(default_limits, dict):
        return None
    by_limit_id = body.get("rateLimitsByLimitId")
    buckets = (
        [
            (limit_id, limits)
            for limit_id, limits in by_limit_id.items()
            if isinstance(limit_id, str) and isinstance(limits, dict)
        ]
        if isinstance(by_limit_id, dict)
        else []
    )
    if not buckets:
        default_id = default_limits.get("limitId")
        buckets = [
            (
                default_id if isinstance(default_id, str) and default_id else "default",
                default_limits,
            )
        ]
    windows: list[dict[str, object]] = []
    limit_states: dict[str, str] = {}
    spend_control_values: list[bool] = []
    for limit_id, raw_limits in buckets:
        metadata = {
            "limit_name": raw_limits.get("limitName"),
            "model": raw_limits.get("normalModelSlug"),
        }
        for name in ("primary", "secondary"):
            raw = raw_limits.get(name)
            if not isinstance(raw, dict):
                continue
            used = _percentage(raw.get("usedPercent"))
            minutes = raw.get("windowDurationMins")
            if (
                used is None
                or isinstance(minutes, bool)
                or not isinstance(minutes, (int, float))
                or not math.isfinite(float(minutes))
                or minutes <= 0
            ):
                continue
            duration = float(minutes)
            window = _window(
                _period(duration),
                used,
                _iso_reset(raw.get("resetsAt")),
                duration,
                limit_id,
            )
            window.update(
                {
                    key: value
                    for key, value in metadata.items()
                    if isinstance(value, str)
                }
            )
            windows.append(window)
        monthly = raw_limits.get("individualLimit")
        if isinstance(monthly, dict):
            remaining = _percentage(monthly.get("remainingPercent"))
            if remaining is not None:
                monthly_window = _window(
                    "monthly",
                    100.0 - remaining,
                    _iso_reset(monthly.get("resetsAt")),
                    limit_id=limit_id,
                )
                monthly_window.update(
                    {
                        key: value
                        for key, value in metadata.items()
                        if isinstance(value, str)
                    }
                )
                for name in ("used", "limit"):
                    value = monthly.get(name)
                    if isinstance(value, str):
                        monthly_window[name] = value
                windows.append(monthly_window)
        reached = raw_limits.get("rateLimitReachedType")
        if isinstance(reached, str):
            limit_states[limit_id] = reached
        spend_control = raw_limits.get("spendControlReached")
        if isinstance(spend_control, bool):
            spend_control_values.append(spend_control)
    ordinary_allowed = body.get("ordinaryUsageAllowed")
    if (
        not windows
        and not isinstance(ordinary_allowed, bool)
        and not spend_control_values
        and not limit_states
    ):
        return None
    snapshot: dict[str, object] = {
        "observed_at": captured_at,
        "source": "provider_api",
        "windows": windows,
    }
    if isinstance(ordinary_allowed, bool):
        snapshot["ordinary_usage_allowed"] = ordinary_allowed
    if spend_control_values:
        snapshot["spend_control_reached"] = any(spend_control_values)
    if limit_states:
        snapshot["limit_states"] = limit_states
        if len(limit_states) == 1:
            snapshot["rate_limit_reached_type"] = next(iter(limit_states.values()))
    return snapshot


def _secure_file(path: Path) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            return None
        if metadata.st_mode & 0o077 or metadata.st_size > 64 * 1024:
            return None
        chunks: list[bytes] = []
        remaining = 64 * 1024 + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        return raw.decode("utf-8") if len(raw) <= 64 * 1024 else None
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(descriptor)


def _claude_token(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    return token if isinstance(token, str) and token else None


def _keychain_credential() -> str | None:
    if sys_platform() != "darwin" or not Path("/usr/bin/security").is_file():
        return None
    accounts = [os.environ.get("USER"), None]
    for account in accounts:
        command = [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            CLAUDE_KEYCHAIN_SERVICE,
        ]
        if account:
            command.extend(("-a", account))
        command.append("-w")
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout:
            return result.stdout
    return None


def sys_platform() -> str:
    return sys.platform


def read_claude_access_token() -> str | None:
    """Read the provider-owned access token without copying or refreshing it."""
    token = _claude_token(_secure_file(Path.home() / ".claude" / ".credentials.json"))
    return token if token is not None else _claude_token(_keychain_credential())


def _retry_after(headers: object) -> float | None:
    getter = getattr(headers, "get", None)
    value: object = getter("Retry-After") if callable(getter) else None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _open_url(request: Request, timeout: float) -> HTTPResponse:
    return cast(
        HTTPResponse, build_opener(_NoRedirect()).open(request, timeout=timeout)
    )


def fetch_claude_limits(
    now: float | None = None,
    opener: OpenURL = _open_url,
    token_reader: Callable[[], str | None] = read_claude_access_token,
) -> FetchResult:
    """Fetch Claude quota once; credentials remain in memory for this request only."""
    token = token_reader()
    if token is None:
        return FetchResult(None, unavailable=True, failure="credentials_unavailable")
    request = Request(
        CLAUDE_USAGE_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "konvu-telemetry",
        },
    )
    try:
        with opener(request, REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return FetchResult(None, failure="invalid_response")
            body = json.loads(raw)
    except HTTPError as error:
        return FetchResult(
            None,
            _retry_after(error.headers) if error.code == 429 else None,
            unavailable=error.code in {401, 403},
            failure=(
                "rate_limited"
                if error.code == 429
                else "authentication_failed"
                if error.code in {401, 403}
                else "provider_error"
            ),
        )
    except (URLError, OSError, TimeoutError):
        return FetchResult(None, failure="network_error")
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return FetchResult(None, failure="invalid_response")
    captured = time.time() if now is None else now
    snapshot = decode_claude_usage(body, _iso_now(captured))
    return FetchResult(
        snapshot,
        failure=None if snapshot is not None else "invalid_response",
    )


def _codex_executable() -> str | None:
    configured = shutil.which("codex")
    if configured is not None:
        return configured
    for candidate in (
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        Path.home() / ".local" / "bin" / "codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _send(process: subprocess.Popen[str], message: dict[str, object]) -> None:
    if process.stdin is None:
        raise OSError("Codex app-server stdin unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _response(
    process: subprocess.Popen[str], request_id: int, deadline: float
) -> dict[str, object] | None:
    if process.stdout is None:
        return None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            events = selector.select(max(0.0, deadline - time.monotonic()))
            if not events:
                return None
            line = process.stdout.readline(MAX_RESPONSE_BYTES + 1)
            if not line or len(line) > MAX_RESPONSE_BYTES:
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


def fetch_codex_limits(now: float | None = None) -> FetchResult:
    """Ask Codex's local app-server for account limits without reading its token."""
    executable = _codex_executable()
    if executable is None:
        return FetchResult(None, unavailable=True, failure="service_unavailable")
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
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        _send(
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
        if _response(process, 1, deadline) is None:
            return FetchResult(None, failure="service_unavailable")
        _send(process, {"method": "initialized"})
        _send(
            process,
            {
                "id": 2,
                "method": "account/rateLimits/read",
                "params": {"excludeResetCreditDetails": True},
            },
        )
        result = _response(process, 2, deadline)
        captured = time.time() if now is None else now
        snapshot = decode_codex_usage(result, _iso_now(captured))
        return FetchResult(
            snapshot,
            failure=None if snapshot is not None else "invalid_response",
        )
    except (OSError, ValueError):
        return FetchResult(None, failure="service_unavailable")
    finally:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass


class ProviderLimitPoller:
    """Poll independently per provider and expose only the latest fresh result."""

    def __init__(
        self,
        claude_fetcher: Callable[[float | None], FetchResult] = fetch_claude_limits,
        codex_fetcher: Callable[[float | None], FetchResult] = fetch_codex_limits,
    ) -> None:
        self._fetchers = {"claude": claude_fetcher, "codex": codex_fetcher}
        self._next_at = {"claude": 0.0, "codex": 0.0}
        self._failures = {"claude": 0, "codex": 0}
        self._snapshots: dict[str, dict[str, object] | None] = {}
        self._last_success_at: dict[str, float] = {}
        self._failure_reasons: dict[str, FailureReason | None] = {}

    def _visible_snapshot(self, provider: str, now: float) -> dict[str, object]:
        snapshot = self._snapshots.get(provider)
        last_success = self._last_success_at.get(provider)
        failure = self._failure_reasons.get(provider)
        if (
            snapshot is not None
            and last_success is not None
            and now - last_success <= RESULT_GRACE_SECONDS
        ):
            if failure is None:
                return snapshot
            visible = dict(snapshot)
            windows = snapshot.get("windows")
            if isinstance(windows, list):
                visible_windows: list[dict[str, object]] = []
                expired_window = False
                for window in windows:
                    if not isinstance(window, dict):
                        continue
                    reset = _iso_reset(window.get("resets_at"))
                    if (
                        reset is not None
                        and datetime.fromisoformat(reset).timestamp() <= now
                    ):
                        expired_window = True
                        continue
                    visible_windows.append(window)
                visible["windows"] = visible_windows
                if expired_window:
                    for key in (
                        "ordinary_usage_allowed",
                        "spend_control_reached",
                        "limit_states",
                        "rate_limit_reached_type",
                    ):
                        visible.pop(key, None)
            visible["status"] = "stale"
            visible["failure"] = failure
            return visible
        return {
            "source": "provider_api",
            "status": "unavailable",
            "failure": failure or "service_unavailable",
            "windows": [],
        }

    def refresh(self, now: float) -> dict[str, object]:
        due = [
            provider for provider, next_at in self._next_at.items() if now >= next_at
        ]
        if due:
            with ThreadPoolExecutor(max_workers=len(due)) as executor:
                futures = {
                    provider: executor.submit(self._fetchers[provider], now)
                    for provider in due
                }
                for provider, future in futures.items():
                    try:
                        result = future.result()
                    except Exception:
                        result = FetchResult(None, failure="service_unavailable")
                    failure = result.failure or (
                        "service_unavailable" if result.snapshot is None else None
                    )
                    if result.snapshot is not None:
                        self._snapshots[provider] = result.snapshot
                        self._last_success_at[provider] = now
                        self._failure_reasons[provider] = None
                    elif result.unavailable:
                        self._snapshots[provider] = None
                        self._last_success_at.pop(provider, None)
                        self._failure_reasons[provider] = failure
                    else:
                        self._failure_reasons[provider] = failure
                    if result.snapshot is not None or result.unavailable:
                        self._failures[provider] = 0
                    else:
                        self._failures[provider] += 1
                    failure_delay = min(
                        15 * 60.0,
                        POLL_INTERVAL_SECONDS
                        * (2 ** max(0, self._failures[provider] - 1)),
                    )
                    delay = max(
                        POLL_INTERVAL_SECONDS,
                        failure_delay,
                        result.retry_after_seconds or 0.0,
                    )
                    self._next_at[provider] = now + delay
        return {
            provider: self._visible_snapshot(provider, now)
            for provider in self._fetchers
        }
