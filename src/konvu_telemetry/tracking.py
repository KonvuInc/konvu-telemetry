"""Minimal, anonymous product analytics for Konvu Telemetry."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
import fcntl
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
from queue import Empty, SimpleQueue
from threading import Lock, Thread
from typing import Callable, Iterator, TypedDict
from urllib.request import Request, urlopen
from uuid import uuid4

from .storage import (
    ensure_private_directory,
    tracking_queue_path,
    tracking_state_path,
    write_private_json,
)

Sender = Callable[[bytes], None]


class QueuedEvent(TypedDict):
    event: str
    properties: dict[str, object]

POSTHOG_PROJECT_KEY = "phc_AKTThCdGvkAkitNvu5cBfoWUbM5gfaBBF67UYNbvj8do"
POSTHOG_BATCH_URL = "https://us.i.posthog.com/batch/"
DELIVERY_TIMEOUT_SECONDS = 0.5
MAX_QUEUED_EVENTS = 100
DURATION_BUCKETS = frozenset(
    {"under_1_second", "under_5_seconds", "under_15_seconds", "15_seconds_or_more"}
)
def _send_to_posthog(payload: bytes) -> None:
    request = Request(
        POSTHOG_BATCH_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=DELIVERY_TIMEOUT_SECONDS) as response:
        if not 200 <= response.status < 300:
            raise OSError("PostHog rejected telemetry batch")


def _cli_version() -> str:
    try:
        return version("konvu-telemetry")
    except PackageNotFoundError:
        return "unknown"


def _os_family() -> str:
    return "macOS" if platform.system() == "Darwin" else platform.system()


@dataclass(frozen=True)
class TrackingStatus:
    enabled: bool


class TrackingStore:
    """Persist the local preference and a small, allowlisted delivery queue."""

    def __init__(
        self,
        state_path: Path,
        queue_path: Path,
        sender: Sender = _send_to_posthog,
    ) -> None:
        self._state_path = state_path
        self._queue_path = queue_path
        self._lock_path = state_path.with_name("tracking.lock")
        self._sender = sender
        self._thread_lock = Lock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            ensure_private_directory(self._lock_path.parent)
            with self._lock_path.open("a") as handle:
                try:
                    self._lock_path.chmod(0o600)
                except OSError:
                    pass
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def status(self) -> TrackingStatus:
        with self._locked():
            return TrackingStatus(enabled=bool(self._state()["enabled"]))

    def set_enabled(self, enabled: bool) -> None:
        with self._locked():
            state = self._state()
            state["enabled"] = enabled
            write_private_json(self._state_path, state)
            if not enabled:
                write_private_json(self._queue_path, [])

    def record(self, event: str, properties: dict[str, object]) -> None:
        normalized = self._normalize_properties(event, properties)
        if normalized is None:
            return
        with self._locked():
            if not bool(self._state()["enabled"]):
                return
            self._append({"event": event, "properties": normalized})

    def record_active_day(self, day: str) -> None:
        with self._locked():
            state = self._state()
            if not bool(state["enabled"]) or state.get("active_day") == day:
                return
            state["active_day"] = day
            write_private_json(self._state_path, state)
            self._append({"event": "telemetry active day", "properties": {}})

    def record_first_snapshot_ready(self) -> None:
        with self._locked():
            state = self._state()
            if not bool(state["enabled"]) or bool(state.get("first_snapshot_ready")):
                return
            state["first_snapshot_ready"] = True
            write_private_json(self._state_path, state)
            self._append({"event": "first snapshot ready", "properties": {}})

    def record_collector_failure(self, day: str) -> None:
        with self._locked():
            state = self._state()
            if not bool(state["enabled"]) or state.get("collector_failure_day") == day:
                return
            state["collector_failure_day"] = day
            write_private_json(self._state_path, state)
            self._append(
                {"event": "collector failed", "properties": {"stage": "snapshot"}}
            )

    def send_queued(self) -> None:
        with self._locked():
            state = self._state()
            queue = self._sanitized_queue()
            if not bool(state["enabled"]) or not queue:
                return
            payload = json.dumps(
                {
                    "api_key": POSTHOG_PROJECT_KEY,
                    "batch": [self._event_payload(event, state["install_id"]) for event in queue],
                }
            ).encode()
        try:
            self._sender(payload)
        except Exception:
            return
        with self._locked():
            if not bool(self._state()["enabled"]):
                return
            current = self._sanitized_queue()
            if current[: len(queue)] == queue:
                write_private_json(self._queue_path, current[len(queue) :])

    def _append(self, event: QueuedEvent) -> None:
        queue = self._sanitized_queue()
        queue.append(event)
        write_private_json(self._queue_path, queue[-MAX_QUEUED_EVENTS:])

    def _event_payload(
        self, queued_event: QueuedEvent, install_id: object
    ) -> dict[str, object]:
        event = queued_event["event"]
        properties = queued_event["properties"]
        return {
            "event": event,
            "properties": {
                "distinct_id": install_id,
                "$process_person_profile": False,
                "$ip": "0",
                "cli_version": _cli_version(),
                "os_family": _os_family(),
                **properties,
            },
        }

    def _state(self) -> dict[str, object]:
        try:
            value = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
            value = {"enabled": True, "install_id": str(uuid4())}
            write_private_json(self._state_path, value)
        elif not isinstance(value.get("install_id"), str):
            value = {"enabled": value["enabled"], "install_id": str(uuid4())}
            write_private_json(self._state_path, value)
        return value

    def _sanitized_queue(self) -> list[QueuedEvent]:
        try:
            value = json.loads(self._queue_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(value, list):
            return []
        sanitized: list[QueuedEvent] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            event = item.get("event")
            properties = item.get("properties")
            if not isinstance(event, str) or not isinstance(properties, dict):
                continue
            normalized = self._normalize_properties(event, properties)
            if normalized is not None:
                sanitized.append({"event": event, "properties": normalized})
        return sanitized[-MAX_QUEUED_EVENTS:]

    @staticmethod
    def _normalize_properties(
        event: str, properties: dict[str, object]
    ) -> dict[str, object] | None:
        if event == "telemetry setup completed":
            bucket = properties.get("duration_bucket")
            return {"duration_bucket": bucket} if bucket in DURATION_BUCKETS else None
        if event == "dashboard opened":
            data_available = properties.get("data_available")
            return {"data_available": data_available} if type(data_available) is bool else None
        if event in {"first snapshot ready", "telemetry active day"}:
            return {}
        if event == "collector failed" and properties.get("stage") == "snapshot":
            return {"stage": "snapshot"}
        return None


_STORE = TrackingStore(tracking_state_path(), tracking_queue_path())
_PENDING_EVENTS: SimpleQueue[tuple[str, dict[str, object]]] = SimpleQueue()
_WORKER_LOCK = Lock()


def _schedule(action: str, properties: dict[str, object]) -> None:
    _PENDING_EVENTS.put((action, properties))
    flush_in_background()


def record_setup_completed(duration_seconds: float) -> None:
    if duration_seconds < 1:
        bucket = "under_1_second"
    elif duration_seconds < 5:
        bucket = "under_5_seconds"
    elif duration_seconds < 15:
        bucket = "under_15_seconds"
    else:
        bucket = "15_seconds_or_more"
    _schedule("telemetry setup completed", {"duration_bucket": bucket})


def record_dashboard_opened(data_available: bool) -> None:
    _schedule("dashboard opened", {"data_available": data_available})
    _schedule("active day", {"day": date.today().isoformat()})


def record_first_snapshot_ready() -> None:
    _schedule("first snapshot ready", {})


def record_collector_failure() -> None:
    _schedule("collector failure", {"day": date.today().isoformat()})


def set_tracking_enabled(enabled: bool) -> None:
    try:
        _STORE.set_enabled(enabled)
    except Exception:
        return


def tracking_status() -> TrackingStatus:
    try:
        return _STORE.status()
    except Exception:
        return TrackingStatus(enabled=False)


def flush_in_background() -> None:
    if not _WORKER_LOCK.acquire(blocking=False):
        return

    def flush() -> None:
        try:
            while True:
                try:
                    action, properties = _PENDING_EVENTS.get_nowait()
                except Empty:
                    break
                if action == "active day":
                    _STORE.record_active_day(str(properties["day"]))
                elif action == "first snapshot ready":
                    _STORE.record_first_snapshot_ready()
                elif action == "collector failure":
                    _STORE.record_collector_failure(str(properties["day"]))
                else:
                    _STORE.record(action, properties)
            _STORE.send_queued()
        except Exception:
            return
        finally:
            _WORKER_LOCK.release()
            if not _PENDING_EVENTS.empty():
                flush_in_background()

    Thread(target=flush, daemon=True).start()
