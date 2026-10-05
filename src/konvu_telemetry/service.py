"""Resident collector loop and localhost dashboard server."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
from socket import socket
from socketserver import BaseServer
import signal
import subprocess
from threading import Condition, Lock, Thread
import time
from typing import IO, Callable, Iterator
from urllib.parse import parse_qs, urlparse
import webbrowser

from .config import (
    ALLOWED_PROVIDERS,
    DEFAULT_HEALTH_STALE_SECONDS,
    LIVE_ACTIVITY_SECONDS,
    package_version,
)
from .context_map import ContextMapScheduler
from .context_drift import ContextDriftScheduler
from .live import IncrementalLiveState
from .provider_limits import (
    ProviderLimitPoller,
    stored_provider_quotas,
    write_provider_quotas,
)
from .snapshot import build_snapshot, write_snapshot
from .preferences import (
    CADENCES,
    custom_rule_prompt,
    read_preferences,
    write_preferences,
)
from .storage import (
    account_quotas_path,
    collection_lock_path,
    collector_lock_path,
    health_path,
    parse_timestamp,
    session_path,
    snapshot_path,
    valid_session_id,
    write_private_json,
)
from .tracking import (
    flush_in_background as flush_tracking_in_background,
    record_collector_failure,
    record_dashboard_opened,
    record_first_snapshot_ready,
)

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
LOGGER = logging.getLogger(__name__)
_DASHBOARD_DATA_AVAILABLE = False
REFRESH_TIMEOUT_SECONDS = 30


# How long a departing collector is given to close its socket and let go.
HANDOVER_TIMEOUT_SECONDS = 15.0
HANDOVER_POLL_SECONDS = 0.25
UPGRADE_RESTART_EXIT_CODE = 75


def _process_is_a_collector(pid: int) -> bool:
    """Whether this pid is really our collector, and not a reused number.

    A pid is recycled the moment its process ends, so the recorded number alone
    is never enough to justify signalling it.
    """
    if pid <= 1 or pid == os.getpid():
        return False
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    command = completed.stdout.strip()
    # Our own package or console script, in either spelling. Matching the word
    # "serve" as well would miss the entry point, which does not carry it.
    return "konvu_telemetry" in command or "konvu-telemetry" in command


def _recorded_lock_holder(handle: IO[str]) -> int | None:
    try:
        handle.seek(0)
        return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _ask_holder_to_stand_down(handle: IO[str]) -> bool:
    """Signal the running collector and wait for its lock, after an upgrade.

    Homebrew leaves the old process running, so without a handover the new one
    exits and the old code keeps serving until someone re-runs setup.
    """
    pid = _recorded_lock_holder(handle)
    if pid is None or not _process_is_a_collector(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return False
    deadline = time.monotonic() + HANDOVER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            time.sleep(HANDOVER_POLL_SECONDS)
    return False


@contextmanager
def collector_process_lock(take_over: bool = False) -> Iterator[None]:
    """Keep a single long-running collector serving the dashboard.

    The service takes the lock over from an older collector after an upgrade.
    A one-shot run never takes it: it serializes with `collection_lock` instead.
    """
    path = collector_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            if not (take_over and _ask_holder_to_stand_down(handle)):
                raise SystemExit(
                    "Another Konvu telemetry collector is already running"
                ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def collection_lock() -> Iterator[None]:
    """Serialize one collection's writes across processes.

    The service holds it only while writing, so `konvu-telemetry once` waits for
    the current collection rather than exiting, and the quota-attribution ledger
    and health record, which every collection reads and rewrites whole, are never
    written by two collectors at once.
    """
    path = collection_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RefreshCoordinator:
    """Coordinate scheduled and requested collections through one worker."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._collecting = False
        self._completed = 0
        self._requested = False
        self._last_error: str | None = None

    def start_collection(self) -> None:
        with self._condition:
            self._collecting = True
            self._requested = False

    def finish_collection(self, error: str | None) -> None:
        with self._condition:
            self._collecting = False
            self._completed += 1
            self._last_error = error
            self._requested = False
            self._condition.notify_all()

    def wait_for_refresh(self, timeout: float) -> bool:
        with self._condition:
            requested = self._condition.wait_for(lambda: self._requested, timeout)
            self._requested = False
            return requested

    def request_refresh(self, timeout: float = REFRESH_TIMEOUT_SECONDS) -> str | None:
        with self._condition:
            completed = self._completed
            if not self._collecting:
                self._requested = True
                self._condition.notify_all()
            refreshed = self._condition.wait_for(
                lambda: self._completed > completed, timeout
            )
            if not refreshed:
                raise TimeoutError("Collector refresh timed out")
            return self._last_error


def snapshot_has_dashboard_data(snapshot: object, now: float) -> bool:
    if not isinstance(snapshot, dict):
        return False
    sessions = snapshot.get("sessions")
    configured_window = snapshot.get("live_activity_window_seconds")
    window = (
        float(configured_window)
        if isinstance(configured_window, (int, float))
        and not isinstance(configured_window, bool)
        and configured_window > 0
        else float(LIVE_ACTIVITY_SECONDS)
    )
    for session in sessions if isinstance(sessions, list) else []:
        if not isinstance(session, dict):
            continue
        last_activity = parse_timestamp(session.get("last_activity_at"))
        if last_activity is None:
            continue
        elapsed = now - last_activity
        if -60 <= elapsed <= window:
            return True
    return False


def active_providers(snapshot: object, now: float) -> frozenset[str]:
    """Return providers with a session active inside the live activity window."""
    if not isinstance(snapshot, dict):
        return frozenset()
    sessions = snapshot.get("sessions")
    providers: set[str] = set()
    for session in sessions if isinstance(sessions, list) else []:
        if not isinstance(session, dict):
            continue
        provider = session.get("provider")
        last_activity = parse_timestamp(session.get("last_activity_at"))
        if (
            provider in ALLOWED_PROVIDERS
            and last_activity is not None
            and -60 <= now - last_activity <= LIVE_ACTIVITY_SECONDS
        ):
            providers.add(provider)
    return frozenset(providers)


def recorded_snapshot() -> dict[str, object] | None:
    """Read the last canonical snapshot for polling cadence decisions."""
    try:
        payload = json.loads(snapshot_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def initialize_dashboard_data_available(now: float | None = None) -> None:
    """Restore dashboard visibility state once when the resident service starts."""
    global _DASHBOARD_DATA_AVAILABLE
    try:
        snapshot = json.loads(snapshot_path().read_text())
    except (OSError, json.JSONDecodeError):
        _DASHBOARD_DATA_AVAILABLE = False
        return
    current_time = time.time() if now is None else now
    _DASHBOARD_DATA_AVAILABLE = snapshot_has_dashboard_data(snapshot, current_time)


def local_request_allowed(host: str, origin: str | None) -> bool:
    host_name = urlparse(f"//{host}").hostname
    origin_host = urlparse(origin).hostname if origin is not None else None
    return host_name in LOCAL_HOSTS and (origin is None or origin_host in LOCAL_HOSTS)


def open_dashboard(port: int) -> None:
    """Open the resident local dashboard."""
    webbrowser.open(f"http://127.0.0.1:{port}/")
    print("Opening the Konvu dashboard")


def write_health(
    now: float, error: str | None = None, interval_seconds: int | None = None
) -> None:
    """Record collector freshness so stale data is never mistaken for live data."""
    previous: dict[str, object] = {}
    try:
        loaded = json.loads(health_path().read_text())
        previous = loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    # Named apart from the installed version because an upgrade leaves the running
    # collector behind the one on disk until it restarts.
    previous["collector_version"] = package_version()
    if error is None:
        previous["status"] = "healthy"
        previous["last_success_at"] = datetime.fromtimestamp(
            now, timezone.utc
        ).isoformat()
        if interval_seconds is not None:
            previous["interval_seconds"] = interval_seconds
            previous["next_poll_at"] = datetime.fromtimestamp(
                now + interval_seconds, timezone.utc
            ).isoformat()
        previous.pop("last_error", None)
        previous.pop("last_error_at", None)
    else:
        previous["status"] = "error"
        previous["last_error"] = error
        previous["last_error_at"] = datetime.fromtimestamp(
            now, timezone.utc
        ).isoformat()
    write_private_json(health_path(), previous)


def load_health(now: float | None = None) -> dict[str, object]:
    try:
        payload = json.loads(health_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"status": "starting"}
    if not isinstance(payload, dict):
        return {"status": "starting"}
    last_success = parse_timestamp(payload.get("last_success_at"))
    interval = payload.get("interval_seconds")
    stale_after = max(
        DEFAULT_HEALTH_STALE_SECONDS,
        int(interval) * 2 + 30 if isinstance(interval, int) and interval > 0 else 0,
    )
    current_time = time.time() if now is None else now
    if payload.get("status") == "healthy" and (
        last_success is None or current_time - last_success > stale_after
    ):
        payload = dict(payload)
        payload["status"] = "stale"
        if last_success is not None:
            payload["stale_for_seconds"] = round(current_time - last_success)
    return payload


def package_was_replaced() -> bool:
    """Whether the code this process is running has been replaced on disk.

    An upgrade installs into a new directory and deletes the old one, but the
    running collector keeps serving from memory. Its own module file vanishing
    is the cheapest reliable signal that it is now the previous version.
    """
    try:
        return not Path(__file__).exists()
    except OSError:
        return False


def collect_forever(
    interval_seconds: int,
    live_state: IncrementalLiveState,
    snapshot_lock: Lock,
    refresh_coordinator: RefreshCoordinator | None = None,
    provider_limit_poller: ProviderLimitPoller | None = None,
    on_stale_install: Callable[[], None] | None = None,
    context_map_collector: ContextMapScheduler | None = None,
    context_drift_collector: ContextDriftScheduler | None = None,
) -> None:
    """Refresh local session files until the operating system stops the service."""
    global _DASHBOARD_DATA_AVAILABLE
    coordinator = refresh_coordinator or RefreshCoordinator()
    quota_poller = provider_limit_poller or ProviderLimitPoller(
        initial_snapshots=stored_provider_quotas()
    )
    context_mapper = context_map_collector or ContextMapScheduler()
    context_drift = context_drift_collector or ContextDriftScheduler()
    while True:
        if package_was_replaced():
            # Exiting hands the service back to launchd, which starts it again
            # from the new install; staying would serve the old code forever.
            # The collector runs in a daemon thread, so the whole process has to
            # be told to stop, not just this loop.
            if on_stale_install is not None:
                on_stale_install()
            return
        coordinator.start_collection()
        started_at = time.time()
        collection_error = None
        try:
            try:
                provider_quotas = quota_poller.refresh(
                    started_at,
                    active_providers(recorded_snapshot(), started_at),
                )
            except Exception:
                provider_quotas = stored_provider_quotas()
            with snapshot_lock, collection_lock():
                provider_quotas = write_provider_quotas(provider_quotas)
                snapshot = build_snapshot(
                    started_at,
                    live_state,
                    provider_quotas=provider_quotas,
                )
                enrich_context_maps(snapshot, live_state, context_mapper)
                context_drift.refresh(snapshot, provider_quotas, started_at)
                write_snapshot(snapshot)
            _DASHBOARD_DATA_AVAILABLE = snapshot_has_dashboard_data(
                snapshot, time.time()
            )
            if _DASHBOARD_DATA_AVAILABLE:
                record_first_snapshot_ready()
            with collection_lock():
                write_health(time.time(), interval_seconds=interval_seconds)
        except Exception as error:
            collection_error = f"{type(error).__name__}: {error}"
            record_collector_failure()
            try:
                with collection_lock():
                    write_health(time.time(), collection_error, interval_seconds)
            except Exception as health_error:
                LOGGER.error(
                    "Collector failed (%s); health write failed (%s)",
                    type(error).__name__,
                    type(health_error).__name__,
                )
        finally:
            coordinator.finish_collection(collection_error)
        coordinator.wait_for_refresh(interval_seconds)


def enrich_context_maps(
    snapshot: dict[str, object],
    live_state: IncrementalLiveState,
    collector: ContextMapScheduler | None = None,
) -> None:
    """Add optional context maps without failing core usage collection."""
    try:
        (collector or ContextMapScheduler()).refresh(snapshot, live_state)
    except Exception as error:
        LOGGER.warning("Context mapping failed: %s", type(error).__name__)


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """Serve the local dashboard and the latest local session snapshot."""

    def __init__(
        self,
        request: socket | tuple[bytes, socket],
        client_address: tuple[str, int],
        server: BaseServer,
        *,
        directory: str | os.PathLike[str] | None = None,
        refresh_coordinator: RefreshCoordinator | None = None,
        snapshot_lock: Lock | None = None,
    ) -> None:
        self.refresh_coordinator = refresh_coordinator
        self.snapshot_lock = snapshot_lock
        super().__init__(request, client_address, server, directory=directory)

    def _write_payload(self, payload: bytes) -> None:
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_json_file(self, path: Path) -> None:
        try:
            with path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                payload = handle.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("ETag", etag)
        self._secure_headers("application/json; charset=utf-8", len(payload))
        self._write_payload(payload)

    def do_GET(self) -> None:  # noqa: N802
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if not local_request_allowed(host, origin):
            self.send_error(403)
            return
        path = urlparse(self.path).path
        if path == "/":
            record_dashboard_opened(data_available=_DASHBOARD_DATA_AVAILABLE)
        if path == "/healthz":
            health = load_health()
            payload = json.dumps(health).encode("utf-8")
            self.send_response(200 if health.get("status") == "healthy" else 503)
            self._secure_headers("application/json; charset=utf-8", len(payload))
            self._write_payload(payload)
            return
        if path == "/api/live-sessions":
            self._serve_live_snapshot()
            return
        if path == "/api/preferences":
            self._send_json(
                {
                    **read_preferences(),
                    "options": [
                        {"id": key, "label": label} for key, label in CADENCES.items()
                    ],
                }
            )
            return
        if path == "/api/session":
            query = parse_qs(urlparse(self.path).query)
            provider = query.get("provider", [""])[0]
            session_id = query.get("session", [""])[0]
            if provider not in ALLOWED_PROVIDERS or not valid_session_id(session_id):
                self.send_error(400)
                return
            self._serve_json_file(session_path(provider, session_id))
            return
        super().do_GET()

    def _serve_live_snapshot(self) -> None:
        """Serve sessions with the independently persisted account quotas."""
        snapshot_file = snapshot_path()
        quotas_file = account_quotas_path()
        try:
            if self.snapshot_lock is None:
                snapshot_stat, snapshot_payload, quota_stat, quota_payload = (
                    DashboardRequestHandler._read_live_files(snapshot_file, quotas_file)
                )
            else:
                with self.snapshot_lock:
                    snapshot_stat, snapshot_payload, quota_stat, quota_payload = (
                        DashboardRequestHandler._read_live_files(
                            snapshot_file, quotas_file
                        )
                    )
            etag_parts = [
                f"{snapshot_stat.st_mtime_ns:x}",
                f"{snapshot_stat.st_size:x}",
            ]
            if quota_stat is not None:
                etag_parts.extend(
                    [f"{quota_stat.st_mtime_ns:x}", f"{quota_stat.st_size:x}"]
                )
            etag = '"' + "-".join(etag_parts) + '"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            snapshot = json.loads(snapshot_payload)
            if not isinstance(snapshot, dict):
                raise ValueError("invalid snapshot")
            if quota_payload is not None:
                quotas = json.loads(quota_payload)
                if not isinstance(quotas, dict):
                    raise ValueError("invalid account quotas")
                snapshot["account_quotas"] = quotas
            payload = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
        except (OSError, json.JSONDecodeError, ValueError):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("ETag", etag)
        self._secure_headers("application/json; charset=utf-8", len(payload))
        self._write_payload(payload)

    @staticmethod
    def _read_live_files(
        snapshot_file: Path, quotas_file: Path
    ) -> tuple[os.stat_result, bytes, os.stat_result | None, bytes | None]:
        with snapshot_file.open("rb") as snapshot_handle:
            snapshot_stat = os.fstat(snapshot_handle.fileno())
            snapshot_payload = snapshot_handle.read()
        try:
            with quotas_file.open("rb") as quota_handle:
                quota_stat = os.fstat(quota_handle.fileno())
                quota_payload = quota_handle.read()
        except FileNotFoundError:
            quota_stat = None
            quota_payload = None
        return snapshot_stat, snapshot_payload, quota_stat, quota_payload

    def do_POST(self) -> None:  # noqa: N802
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if not local_request_allowed(host, origin):
            self.send_error(403)
            return
        path = urlparse(self.path).path
        if path == "/api/preferences":
            self._save_preferences()
            return
        if path != "/api/refresh" or self.refresh_coordinator is None:
            self.send_error(404)
            return
        try:
            error = self.refresh_coordinator.request_refresh()
        except TimeoutError as timeout_error:
            self.send_error(503, str(timeout_error))
            return
        if error is not None:
            self.send_error(503, "Collector refresh failed")
            return
        payload = json.dumps(load_health()).encode("utf-8")
        self.send_response(200)
        self._secure_headers("application/json; charset=utf-8", len(payload))
        self._write_payload(payload)

    def _send_json(self, value: object, status: int = 200) -> None:
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self._secure_headers("application/json; charset=utf-8", len(payload))
        self._write_payload(payload)

    def _save_preferences(self) -> None:
        """Store a cadence chosen in the dashboard. Rejects unknown cadences
        rather than silently falling back, so the UI cannot drift from what is
        actually in force."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if length <= 0 or length > 8192:
            self.send_error(400)
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, OSError):
            self.send_error(400)
            return
        if not isinstance(body, dict):
            self.send_error(400)
            return
        cadence = body.get("cadence")
        if not isinstance(cadence, str) or cadence not in CADENCES:
            self.send_error(400)
            return
        rule = body.get("custom_rule")
        jump = body.get("jump_percent")
        analysis_enabled = body.get("context_analysis_enabled")
        allow_paid = body.get("context_analysis_allow_paid")
        if (analysis_enabled is not None and not isinstance(analysis_enabled, bool)) or (
            allow_paid is not None and not isinstance(allow_paid, bool)
        ):
            self.send_error(400)
            return
        try:
            preference = write_preferences(
                cadence,
                rule if isinstance(rule, str) else "",
                jump
                if isinstance(jump, (int, float)) and not isinstance(jump, bool)
                else None,
                context_analysis_enabled=analysis_enabled,
                context_analysis_allow_paid=allow_paid,
            )
        except (ValueError, OSError):
            self.send_error(400)
            return
        self._send_json(
            {
                **preference,
                "agent_prompt": custom_rule_prompt(preference["custom_rule"])
                if preference["cadence"] == "custom"
                else "",
            }
        )

    def _secure_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.end_headers()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _run_local_service(interval_seconds: int, port: int) -> None:
    directory = Path(__file__).with_name("dashboard")
    if not directory.is_dir():
        raise SystemExit(f"Dashboard files are missing from {directory}")
    live_state = IncrementalLiveState()
    snapshot_lock = Lock()
    refresh_coordinator = RefreshCoordinator()
    context_drift = ContextDriftScheduler()
    handler = partial(
        DashboardRequestHandler,
        directory=str(directory),
        refresh_coordinator=refresh_coordinator,
        snapshot_lock=snapshot_lock,
    )
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        raise SystemExit(f"Konvu dashboard port {port} is already in use") from None
    stale_install = False

    def stop_for_upgrade() -> None:
        nonlocal stale_install
        stale_install = True
        context_drift.close()
        server.shutdown()

    def stop_for_signal(_signum: int, _frame: object) -> None:
        context_drift.close()
        Thread(target=server.shutdown, daemon=True).start()

    collector = Thread(
        target=collect_forever,
        args=(interval_seconds, live_state, snapshot_lock, refresh_coordinator),
        # shutdown() must be called from another thread than serve_forever.
        kwargs={
            "on_stale_install": stop_for_upgrade,
            "context_drift_collector": context_drift,
        },
        daemon=True,
    )
    initialize_dashboard_data_available()
    collector.start()
    flush_tracking_in_background()
    print(
        f"Konvu dashboard running at http://127.0.0.1:{port}/; "
        "use `konvu-telemetry dashboard` to open it"
    )
    previous_sigterm = signal.signal(signal.SIGTERM, stop_for_signal)
    try:
        server.serve_forever()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        context_drift.close()
        server.server_close()
    if stale_install:
        # Existing launch agents restart only after an unsuccessful exit.
        raise SystemExit(UPGRADE_RESTART_EXIT_CODE)


def run_local_service(interval_seconds: int, port: int) -> None:
    """Run the sole collector and localhost dashboard process."""
    with collector_process_lock(take_over=True):
        _run_local_service(interval_seconds, port)
