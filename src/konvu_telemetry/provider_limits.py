"""Read provider-reported account limits without retaining credentials."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import random
import selectors
import stat
import subprocess
import sys
import time
from typing import Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .outbound import HTTPResponse, open_without_redirects
from .storage import (
    account_quotas_path,
    snapshot_path,
    write_private_json_if_changed,
)


CLAUDE_USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_USAGE_USER_AGENT = "claude-code/2.1.0"
CODEX_USAGE_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
POLL_INTERVAL_SECONDS = 120.0
CLAUDE_ACTIVE_POLL_INTERVAL_SECONDS = POLL_INTERVAL_SECONDS
CLAUDE_IDLE_POLL_INTERVAL_SECONDS = 5 * 60.0
RATE_LIMIT_BACKOFF_SECONDS = 20.0
RATE_LIMIT_MAX_BACKOFF_SECONDS = 30 * 60.0
TRANSIENT_FAILURE_MAX_BACKOFF_SECONDS = 15 * 60.0
RATE_LIMIT_JITTER_FRACTION = 0.1
RATE_LIMIT_MAX_JITTER_SECONDS = 5 * 60.0
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 256 * 1024
PIPE_CHUNK_BYTES = 64 * 1024
LOGGER = logging.getLogger(__name__)

FailureReason = Literal[
    "authentication_failed",
    "credentials_unavailable",
    "invalid_response",
    "network_error",
    "provider_error",
    "rate_limited",
    "service_unavailable",
]


OpenURL = Callable[[Request, float], HTTPResponse]


@dataclass(frozen=True)
class FetchResult:
    snapshot: dict[str, object] | None
    retry_after_seconds: float | None = None
    unavailable: bool = False
    failure: FailureReason | None = None
    error_code: int | None = None


@dataclass(frozen=True)
class RPCResponse:
    result: dict[str, object] | None = None
    error_code: int | None = None


@dataclass(frozen=True)
class CodexCredentials:
    access_token: str
    account_id: str | None


def stored_provider_quotas() -> dict[str, object]:
    """Read authoritative provider quotas, including the legacy snapshot location."""
    accounts: object = _read_json_object(account_quotas_path())
    if accounts is None:
        snapshot = _read_json_object(snapshot_path())
        accounts = (
            snapshot.get("account_quotas") if isinstance(snapshot, dict) else None
        )
    if not isinstance(accounts, dict):
        return {}
    return {
        provider: account
        for provider in ("claude", "codex")
        if isinstance((account := accounts.get(provider)), dict)
        and account.get("source") == "provider_api"
    }


def write_provider_quotas(quotas: dict[str, object]) -> dict[str, object]:
    """Persist the collector's provider quota view and return what is now stored.

    `once` and the service both poll before serializing their writes, so the
    later writer may hold the older poll; the most recent poll stays on disk and
    the writer builds its snapshot from the returned view.
    """
    stored = _read_json_object(account_quotas_path()) or {}
    normalized: dict[str, object] = {}
    for provider in ("claude", "codex"):
        quota = quotas.get(provider)
        if not isinstance(quota, dict):
            continue
        previous = stored.get(provider)
        if isinstance(previous, dict) and _polled_before(quota, previous):
            quota = previous
        normalized[provider] = quota
    write_private_json_if_changed(account_quotas_path(), normalized)
    return normalized


def _last_polled_at(quota: dict[str, object]) -> float | None:
    poll_state = quota.get("poll_state")
    polled = poll_state.get("last_polled_at") if isinstance(poll_state, dict) else None
    if isinstance(polled, bool) or not isinstance(polled, (int, float)):
        return None
    return float(polled)


def _polled_before(quota: dict[str, object], other: dict[str, object]) -> bool:
    """Whether both quotas record a poll time and `quota` comes from the older poll."""
    polled = _last_polled_at(quota)
    other_polled = _last_polled_at(other)
    return polled is not None and other_polled is not None and polled < other_polled


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _iso_now(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def _percentage(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    percentage = float(value)
    if not math.isfinite(percentage) or percentage < 0:
        return None
    # Providers report slightly over 100 once a window is exhausted; keep it.
    return min(percentage, 100.0)


def _remaining_percentage(value: object) -> float | None:
    """Validate a remaining figure, where overshoot below zero means exhausted."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    remaining = float(value)
    return _percentage(max(0.0, remaining)) if math.isfinite(remaining) else None


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
            windows.append(
                _window(
                    _period(duration),
                    used,
                    _iso_reset(raw.get("resetsAt")),
                    duration,
                    limit_id,
                )
            )
        monthly = raw_limits.get("individualLimit")
        if isinstance(monthly, dict):
            remaining = _remaining_percentage(monthly.get("remainingPercent"))
            if remaining is not None:
                windows.append(
                    _window(
                        "monthly",
                        100.0 - remaining,
                        _iso_reset(monthly.get("resetsAt")),
                        limit_id=limit_id,
                    )
                )
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


def decode_codex_wham_usage(body: object, captured_at: str) -> dict[str, object] | None:
    """Normalize Codex's direct ChatGPT subscription-usage response."""
    if not isinstance(body, dict):
        return None
    rate_limit = body.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    windows: list[dict[str, object]] = []
    for name in ("primary_window", "secondary_window"):
        raw = rate_limit.get(name)
        if not isinstance(raw, dict):
            continue
        used = _percentage(raw.get("used_percent"))
        seconds = raw.get("limit_window_seconds")
        if (
            used is None
            or isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or seconds <= 0
        ):
            continue
        minutes = float(seconds) / 60.0
        windows.append(
            _window(
                _period(minutes),
                used,
                _iso_reset(raw.get("reset_at")),
                minutes,
                "codex",
            )
        )
    if not windows:
        return None
    return {
        "observed_at": captured_at,
        "source": "provider_api",
        "windows": windows,
    }


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


def _codex_credentials(raw: str | None) -> CodexCredentials | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("auth_mode") != "chatgpt":
        return None
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    account_id = tokens.get("account_id")
    return CodexCredentials(
        token, account_id if isinstance(account_id, str) and account_id else None
    )


def read_codex_credentials() -> CodexCredentials | None:
    """Read the private Codex CLI OAuth credential without refreshing or storing it."""
    return _codex_credentials(_secure_file(Path.home() / ".codex" / "auth.json"))


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


def fetch_claude_limits(
    now: float | None = None,
    opener: OpenURL = open_without_redirects,
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
            "User-Agent": CLAUDE_USAGE_USER_AGENT,
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
            error_code=error.code,
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
    for candidate in (
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        Path.home() / ".local" / "bin" / "codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ):
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid in {0, os.getuid()}
            and not metadata.st_mode & 0o022
            and os.access(resolved, os.X_OK)
        ):
            return str(resolved)
    return None


def _send(process: subprocess.Popen[bytes], message: dict[str, object]) -> None:
    if process.stdin is None:
        raise OSError("Codex app-server stdin unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    process.stdin.flush()


def _read_line(
    process: subprocess.Popen[bytes], deadline: float, pending: bytearray
) -> bytes | None:
    """Return the next record, or None at the deadline, at EOF, or past the size limit."""
    if process.stdout is None:
        return None
    while True:
        end = pending.find(b"\n")
        if end >= 0:
            line = bytes(pending[: end + 1])
            del pending[: end + 1]
            return line if len(line) <= MAX_RESPONSE_BYTES else None
        if len(pending) > MAX_RESPONSE_BYTES:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # Read the raw descriptor so the selector and the buffer agree on what is pending.
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(remaining):
                return None
        chunk = os.read(process.stdout.fileno(), PIPE_CHUNK_BYTES)
        if not chunk:
            return None
        pending += chunk


def _response(
    process: subprocess.Popen[bytes],
    request_id: int,
    deadline: float,
    pending: bytearray,
) -> RPCResponse | None:
    while True:
        line = _read_line(process, deadline, pending)
        if line is None:
            return None
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        result = message.get("result")
        if isinstance(result, dict):
            return RPCResponse(result=result)
        error = message.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
        return RPCResponse(
            error_code=(
                error_code
                if isinstance(error_code, int) and not isinstance(error_code, bool)
                else None
            )
        )


def fetch_codex_direct_limits(
    now: float | None = None,
    opener: OpenURL = open_without_redirects,
    credential_reader: Callable[[], CodexCredentials | None] = read_codex_credentials,
) -> FetchResult:
    """Fetch Codex's weekly quota directly with the existing local credential."""
    credentials = credential_reader()
    if credentials is None:
        return FetchResult(None, unavailable=True, failure="credentials_unavailable")
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Accept": "application/json",
        "User-Agent": "konvu-telemetry",
    }
    if credentials.account_id is not None:
        headers["ChatGPT-Account-Id"] = credentials.account_id
    request = Request(CODEX_USAGE_ENDPOINT, headers=headers)
    try:
        with opener(request, REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return FetchResult(None, failure="invalid_response")
            body = json.loads(raw)
        captured = time.time() if now is None else now
        snapshot = decode_codex_wham_usage(body, _iso_now(captured))
        return FetchResult(
            snapshot,
            failure=None if snapshot is not None else "invalid_response",
        )
    except HTTPError as error:
        return FetchResult(
            None,
            _retry_after(error.headers) if error.code == 429 else None,
            unavailable=error.code in {401, 403},
            failure="rate_limited"
            if error.code == 429
            else "authentication_failed"
            if error.code in {401, 403}
            else "provider_error",
            error_code=error.code,
        )
    except (URLError, OSError, TimeoutError):
        return FetchResult(None, failure="network_error")
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return FetchResult(None, failure="invalid_response")


def fetch_codex_app_server_limits(now: float | None = None) -> FetchResult:
    """Ask Codex's local app-server for its complete account-limit snapshot."""
    executable = _codex_executable()
    if executable is None:
        return FetchResult(None, unavailable=True, failure="service_unavailable")
    process: subprocess.Popen[bytes] | None = None
    pending = bytearray()
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        _send(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "konvu-telemetry", "version": "0.3"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        initialized = _response(process, 1, deadline, pending)
        if initialized is None or initialized.result is None:
            return FetchResult(None, failure="service_unavailable")
        _send(process, {"method": "initialized"})
        _send(
            process,
            {
                "id": 2,
                "method": "account/read",
                "params": {"refreshToken": False},
            },
        )
        account_response = _response(process, 2, deadline, pending)
        if account_response is None:
            return FetchResult(None, failure="service_unavailable")
        if account_response.result is None:
            return FetchResult(
                None,
                unavailable=account_response.error_code in {401, 403},
                failure=(
                    "authentication_failed"
                    if account_response.error_code in {401, 403}
                    else "provider_error"
                ),
                error_code=account_response.error_code,
            )
        if (
            account_response.result.get("account") is None
            and account_response.result.get("requiresOpenaiAuth") is True
        ):
            return FetchResult(
                None,
                unavailable=True,
                failure="credentials_unavailable",
            )
        _send(
            process,
            {
                "id": 3,
                "method": "account/rateLimits/read",
                "params": {"excludeResetCreditDetails": True},
            },
        )
        response = _response(process, 3, deadline, pending)
        if response is None:
            return FetchResult(None, failure="service_unavailable")
        if response.result is None:
            return FetchResult(
                None,
                failure="provider_error",
                error_code=response.error_code,
            )
        captured = time.time() if now is None else now
        snapshot = decode_codex_usage(response.result, _iso_now(captured))
        return FetchResult(
            snapshot,
            failure=None if snapshot is not None else "invalid_response",
        )
    except (OSError, ValueError):
        return FetchResult(None, failure="service_unavailable")
    finally:
        if process is not None:
            _stop_process(process)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def fetch_codex_limits(now: float | None = None) -> FetchResult:
    """Prefer Codex's complete local snapshot and fall back to direct weekly quota."""
    app_server = fetch_codex_app_server_limits(now)
    if app_server.snapshot is not None:
        return app_server
    return fetch_codex_direct_limits(now)


def _rate_limit_delay(failures: int, retry_after_seconds: float | None) -> float:
    """Back off from 20 seconds, doubling per consecutive refusal."""
    exponent = min(max(0, failures - 1), 12)
    delay: float = min(
        RATE_LIMIT_MAX_BACKOFF_SECONDS,
        RATE_LIMIT_BACKOFF_SECONDS * float(2**exponent),
    )
    delay = max(delay, retry_after_seconds or 0.0)
    jitter = min(delay * RATE_LIMIT_JITTER_FRACTION, RATE_LIMIT_MAX_JITTER_SECONDS)
    return delay + float(random.uniform(0.0, jitter))


class ProviderLimitPoller:
    """Poll each provider while safely retaining the latest authoritative result."""

    def __init__(
        self,
        claude_fetcher: Callable[[float | None], FetchResult] = fetch_claude_limits,
        codex_fetcher: Callable[[float | None], FetchResult] = fetch_codex_limits,
        initial_snapshots: dict[str, object] | None = None,
    ) -> None:
        self._fetchers = {"claude": claude_fetcher, "codex": codex_fetcher}
        self._next_at = {"claude": 0.0, "codex": 0.0}
        self._last_polled_at: dict[str, float | None] = {
            "claude": None,
            "codex": None,
        }
        self._failures = {"claude": 0, "codex": 0}
        self._snapshots: dict[str, dict[str, object] | None] = {}
        self._failure_reasons: dict[str, FailureReason | None] = {}
        self._error_codes: dict[str, int | None] = {}
        for provider in self._fetchers:
            initial = (
                initial_snapshots.get(provider)
                if isinstance(initial_snapshots, dict)
                else None
            )
            if not isinstance(initial, dict) or initial.get("source") != "provider_api":
                continue
            canonical = dict(initial)
            poll_state = canonical.pop("poll_state", None)
            if isinstance(poll_state, dict):
                next_at = poll_state.get("next_at")
                last_polled_at = poll_state.get("last_polled_at")
                failures = poll_state.get("failures")
                if isinstance(next_at, (int, float)) and not isinstance(next_at, bool):
                    self._next_at[provider] = max(0.0, float(next_at))
                if isinstance(last_polled_at, (int, float)) and not isinstance(
                    last_polled_at, bool
                ):
                    self._last_polled_at[provider] = max(0.0, float(last_polled_at))
                if isinstance(failures, int) and not isinstance(failures, bool):
                    self._failures[provider] = max(0, failures)
            observed = _iso_reset(canonical.get("observed_at"))
            if observed is None:
                continue
            for display_field in ("status", "failure", "error_code"):
                canonical.pop(display_field, None)
            self._snapshots[provider] = canonical

    def _poll_state(self, provider: str) -> dict[str, object]:
        return {
            "next_at": self._next_at[provider],
            "last_polled_at": self._last_polled_at[provider],
            "failures": self._failures[provider],
        }

    def _with_poll_state(
        self, provider: str, account: dict[str, object]
    ) -> dict[str, object]:
        visible = dict(account)
        visible["poll_state"] = self._poll_state(provider)
        return visible

    def _visible_snapshot(self, provider: str, now: float) -> dict[str, object]:
        snapshot = self._snapshots.get(provider)
        failure = self._failure_reasons.get(provider)
        error_code = self._error_codes.get(provider)
        if snapshot is not None:
            visible = dict(snapshot)
            windows = snapshot.get("windows")
            visible_windows: list[dict[str, object]] = []
            expired_window = False
            if isinstance(windows, list):
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
            if visible_windows:
                if failure is not None:
                    visible["status"] = "stale"
                    visible["failure"] = failure
                    if error_code is not None:
                        visible["error_code"] = error_code
                return self._with_poll_state(provider, visible)
            if failure is None and not expired_window:
                return self._with_poll_state(provider, visible)
        unavailable: dict[str, object] = {
            "source": "provider_api",
            "status": (
                "fetching"
                if failure != "credentials_unavailable" and self._failures[provider] < 3
                else "unavailable"
            ),
            "failure": failure or "service_unavailable",
            "windows": [],
        }
        if error_code is not None:
            unavailable["error_code"] = error_code
        return self._with_poll_state(provider, unavailable)

    def refresh(
        self,
        now: float,
        active_providers: frozenset[str] = frozenset({"claude", "codex"}),
    ) -> dict[str, object]:
        """Refresh due provider limits, polling Claude less often while idle."""

        def is_due(provider: str) -> bool:
            if provider == "claude" and self._failures[provider] == 0:
                interval = (
                    CLAUDE_ACTIVE_POLL_INTERVAL_SECONDS
                    if provider in active_providers
                    else CLAUDE_IDLE_POLL_INTERVAL_SECONDS
                )
                previous = self._last_polled_at[provider]
                return previous is None or now - previous >= interval
            return now >= self._next_at[provider]

        due = [provider for provider in self._fetchers if is_due(provider)]
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
                    self._last_polled_at[provider] = now
                    temporary_auth_failure = (
                        failure == "authentication_failed"
                        and self._failures[provider] + 1 < 3
                    )
                    if result.snapshot is not None:
                        self._snapshots[provider] = result.snapshot
                        self._failure_reasons[provider] = None
                        self._error_codes[provider] = None
                    elif result.unavailable and not temporary_auth_failure:
                        self._snapshots[provider] = None
                        self._failure_reasons[provider] = failure
                        self._error_codes[provider] = result.error_code
                    else:
                        self._failure_reasons[provider] = failure
                        self._error_codes[provider] = result.error_code
                    if result.snapshot is not None:
                        self._failures[provider] = 0
                    elif (
                        result.unavailable
                        and failure == "authentication_failed"
                        and not temporary_auth_failure
                    ):
                        self._failures[provider] = max(3, self._failures[provider] + 1)
                    else:
                        self._failures[provider] += 1
                    if result.snapshot is not None:
                        delay = (
                            (
                                CLAUDE_ACTIVE_POLL_INTERVAL_SECONDS
                                if provider in active_providers
                                else CLAUDE_IDLE_POLL_INTERVAL_SECONDS
                            )
                            if provider == "claude"
                            else POLL_INTERVAL_SECONDS
                        )
                    else:
                        failure_delay = min(
                            TRANSIENT_FAILURE_MAX_BACKOFF_SECONDS,
                            POLL_INTERVAL_SECONDS
                            * (2 ** max(0, self._failures[provider] - 1)),
                        )
                        if provider == "claude":
                            failure_delay = max(
                                failure_delay, CLAUDE_ACTIVE_POLL_INTERVAL_SECONDS
                            )
                        if result.failure == "rate_limited":
                            delay = _rate_limit_delay(
                                self._failures[provider], result.retry_after_seconds
                            )
                        else:
                            delay = max(
                                POLL_INTERVAL_SECONDS,
                                failure_delay,
                                result.retry_after_seconds or 0.0,
                            )
                    self._next_at[provider] = now + delay
                    if failure is not None:
                        LOGGER.warning(
                            "Provider limit request failed: provider=%s failure=%s status=%s retry_after=%s",
                            provider,
                            failure,
                            result.error_code,
                            result.retry_after_seconds,
                        )
        return {
            provider: self._visible_snapshot(provider, now)
            for provider in self._fetchers
        }
