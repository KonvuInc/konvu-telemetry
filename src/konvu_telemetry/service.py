"""Resident collector loop and localhost dashboard server."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
from threading import Lock, Thread
import time
from urllib.parse import parse_qs, urlparse
import webbrowser

from .config import ALLOWED_PROVIDERS, DEFAULT_HEALTH_STALE_SECONDS
from .live import IncrementalLiveState
from .snapshot import build_snapshot, write_snapshot
from .storage import (
    health_path,
    parse_timestamp,
    session_path,
    snapshot_path,
    valid_session_id,
    write_private_json,
)

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
LOGGER = logging.getLogger(__name__)


def local_request_allowed(host: str, origin: str | None) -> bool:
    host_name = urlparse(f"//{host}").hostname
    origin_host = urlparse(origin).hostname if origin is not None else None
    return host_name in LOCAL_HOSTS and (origin is None or origin_host in LOCAL_HOSTS)


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
    if error is None:
        previous["status"] = "healthy"
        previous["last_success_at"] = datetime.fromtimestamp(
            now, timezone.utc
        ).isoformat()
        if interval_seconds is not None:
            previous["interval_seconds"] = interval_seconds
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


def collect_forever(
    interval_seconds: int, live_state: IncrementalLiveState, snapshot_lock: Lock
) -> None:
    """Refresh local session files until the operating system stops the service."""
    while True:
        now = time.time()
        try:
            with snapshot_lock:
                write_snapshot(build_snapshot(now, live_state))
            write_health(now, interval_seconds=interval_seconds)
        except Exception as error:
            try:
                write_health(now, f"{type(error).__name__}: {error}", interval_seconds)
            except Exception as health_error:
                LOGGER.error(
                    "Collector failed (%s); health write failed (%s)",
                    type(error).__name__,
                    type(health_error).__name__,
                )
        time.sleep(interval_seconds)


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """Serve the local dashboard and the latest local session snapshot."""

    live_state: IncrementalLiveState
    snapshot_lock: Lock

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
        if path == "/healthz":
            health = load_health()
            payload = json.dumps(health).encode("utf-8")
            self.send_response(200 if health.get("status") == "healthy" else 503)
            self._secure_headers("application/json; charset=utf-8", len(payload))
            self._write_payload(payload)
            return
        if path == "/api/live-sessions":
            self._serve_json_file(snapshot_path())
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
        if path == "/api/refresh":
            query = parse_qs(urlparse(self.path).query)
            provider = query.get("provider", [""])[0]
            session_id = query.get("session", [""])[0]
            if provider not in ALLOWED_PROVIDERS or not valid_session_id(session_id):
                self.send_error(400)
                return
            with self.snapshot_lock:
                write_snapshot(build_snapshot(time.time(), self.live_state))
                try:
                    candidate = json.loads(
                        session_path(provider, session_id).read_text()
                    )
                    session = candidate if isinstance(candidate, dict) else None
                except (OSError, json.JSONDecodeError):
                    session = None
            payload = json.dumps({"session": session}).encode("utf-8")
            self.send_response(200)
            self._secure_headers("application/json; charset=utf-8", len(payload))
            self._write_payload(payload)
            return
        super().do_GET()

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


def run_local_service(interval_seconds: int, port: int, open_browser: bool) -> None:
    """Run the collector and localhost dashboard together in one process."""
    directory = Path(__file__).with_name("dashboard")
    if not directory.is_dir():
        raise SystemExit(f"Dashboard files are missing from {directory}")
    live_state = IncrementalLiveState()
    snapshot_lock = Lock()
    handler = partial(DashboardRequestHandler, directory=str(directory))
    DashboardRequestHandler.live_state = live_state
    DashboardRequestHandler.snapshot_lock = snapshot_lock
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    collector = Thread(
        target=collect_forever,
        args=(interval_seconds, live_state, snapshot_lock),
        daemon=True,
    )
    collector.start()
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        webbrowser.open(url)
    print(f"Konvu dashboard running at {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
