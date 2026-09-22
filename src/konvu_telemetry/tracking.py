"""Minimal, anonymous product analytics for Konvu Telemetry."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import fcntl
import json
from pathlib import Path
import platform
from queue import Empty, SimpleQueue
from threading import Lock, Thread
import time
from typing import Callable, Iterator, Literal, TypedDict
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from .storage import (
    ensure_private_directory,
    tracking_queue_path,
    tracking_state_path,
    write_private_json,
)

Sender = Callable[[bytes], None]
Clock = Callable[[], float]
StateKind = Literal["valid", "missing", "invalid"]


class QueuedEvent(TypedDict):
    event: str
    properties: dict[str, object]
    timestamp: str
    uuid: str


POSTHOG_PROJECT_KEY = "phc_AKTThCdGvkAkitNvu5cBfoWUbM5gfaBBF67UYNbvj8do"
POSTHOG_BATCH_URL = "https://us.i.posthog.com/batch/"
DELIVERY_TIMEOUT_SECONDS = 0.5
INITIAL_RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 24 * 60 * 60
MAX_QUEUED_EVENTS = 100
PROTECTED_EVENTS = frozenset({"telemetry setup completed", "first snapshot ready"})
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
        if response.geturl() != POSTHOG_BATCH_URL:
            raise OSError("PostHog redirected telemetry batch")
        if not 200 <= response.status < 300:
            raise OSError("PostHog rejected telemetry batch")


def _cli_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

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
        clock: Clock = time.time,
    ) -> None:
        self._state_path = state_path
        self._queue_path = queue_path
        self._lock_path = state_path.with_name("tracking.lock")
        self._sender = sender
        self._clock = clock
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

    def initialize(self, default_enabled: bool) -> TrackingStatus:
        """Create state only when none exists, preserving invalid state as disabled."""
        with self._locked():
            kind, state = self._read_state()
            if kind == "valid":
                return TrackingStatus(enabled=bool(state["enabled"]))
            if kind == "invalid":
                return TrackingStatus(enabled=False)
            state = self._new_state(default_enabled)
            write_private_json(self._state_path, state)
            return TrackingStatus(enabled=default_enabled)

    def status(self) -> TrackingStatus:
        with self._locked():
            kind, state = self._read_state()
            return TrackingStatus(
                enabled=kind == "valid" and bool(state.get("enabled"))
            )

    def set_enabled(self, enabled: bool) -> None:
        with self._locked():
            kind, state = self._read_state()
            if kind != "valid":
                state = self._new_state(enabled)
            else:
                state = dict(state)
                state["enabled"] = enabled
            if not enabled:
                state.pop("delivery_failures", None)
                state.pop("next_send_at", None)
            write_private_json(self._state_path, state)
            if not enabled:
                write_private_json(self._queue_path, [])

    def record(self, event: str, properties: dict[str, object]) -> None:
        normalized = self._normalize_properties(event, properties)
        if normalized is None:
            return
        with self._locked():
            kind, state = self._read_state()
            if kind != "valid" or not bool(state["enabled"]):
                return
            self._append(event, normalized)

    def record_setup_completed(self, duration_bucket: str) -> None:
        self._record_once(
            "telemetry setup completed",
            {"duration_bucket": duration_bucket},
            "setup_delivered",
        )

    def record_first_snapshot_ready(self) -> None:
        self._record_once("first snapshot ready", {}, "first_snapshot_delivered")

    def _record_once(
        self, event: str, properties: dict[str, object], delivered_key: str
    ) -> None:
        with self._locked():
            kind, state = self._read_state()
            if (
                kind != "valid"
                or not bool(state["enabled"])
                or bool(state.get(delivered_key))
            ):
                return
            queue = self._sanitized_queue()
            if any(item["event"] == event for item in queue):
                return
            self._append(event, properties, queue)

    def record_active_day(self, day: str) -> None:
        with self._locked():
            kind, state = self._read_state()
            if (
                kind != "valid"
                or not bool(state["enabled"])
                or state.get("active_day") == day
            ):
                return
            state = dict(state)
            state["active_day"] = day
            write_private_json(self._state_path, state)
            self._append("telemetry active day", {})

    def record_collector_failure(self, day: str) -> None:
        with self._locked():
            kind, state = self._read_state()
            if (
                kind != "valid"
                or not bool(state["enabled"])
                or state.get("collector_failure_day") == day
            ):
                return
            state = dict(state)
            state["collector_failure_day"] = day
            write_private_json(self._state_path, state)
            self._append("collector failed", {"stage": "snapshot"})

    def send_queued(self) -> None:
        with self._locked():
            kind, state = self._read_state()
            queue = self._sanitized_queue()
            next_send_at = state.get("next_send_at")
            if (
                kind != "valid"
                or not bool(state["enabled"])
                or not queue
                or (
                    isinstance(next_send_at, (int, float))
                    and not isinstance(next_send_at, bool)
                    and self._clock() < next_send_at
                )
            ):
                return
            payload = json.dumps(
                {
                    "api_key": POSTHOG_PROJECT_KEY,
                    "batch": [
                        self._event_payload(event, state["install_id"])
                        for event in queue
                    ],
                }
            ).encode()
        try:
            self._sender(payload)
        except Exception:
            self._record_delivery_failure()
            return
        with self._locked():
            kind, state = self._read_state()
            if kind != "valid" or not bool(state["enabled"]):
                return
            current = self._sanitized_queue()
            if current[: len(queue)] != queue:
                return
            state = dict(state)
            if any(item["event"] == "telemetry setup completed" for item in queue):
                state["setup_delivered"] = True
            if any(item["event"] == "first snapshot ready" for item in queue):
                state["first_snapshot_delivered"] = True
            state.pop("delivery_failures", None)
            state.pop("next_send_at", None)
            write_private_json(self._state_path, state)
            write_private_json(self._queue_path, current[len(queue) :])

    def _record_delivery_failure(self) -> None:
        with self._locked():
            kind, state = self._read_state()
            if kind != "valid" or not bool(state["enabled"]):
                return
            previous = state.get("delivery_failures")
            failures = previous + 1 if isinstance(previous, int) else 1
            delay = min(
                INITIAL_RETRY_SECONDS * (2 ** min(failures - 1, 10)),
                MAX_RETRY_SECONDS,
            )
            state = dict(state)
            state["delivery_failures"] = failures
            state["next_send_at"] = self._clock() + delay
            try:
                write_private_json(self._state_path, state)
            except OSError:
                pass

    def _append(
        self,
        event: str,
        properties: dict[str, object],
        queue: list[QueuedEvent] | None = None,
    ) -> None:
        current = self._sanitized_queue() if queue is None else queue
        current.append(
            {
                "event": event,
                "properties": properties,
                "timestamp": datetime.fromtimestamp(
                    self._clock(), timezone.utc
                ).isoformat(),
                "uuid": str(uuid4()),
            }
        )
        while len(current) > MAX_QUEUED_EVENTS:
            removable = next(
                (
                    index
                    for index, item in enumerate(current)
                    if item["event"] not in PROTECTED_EVENTS
                ),
                0,
            )
            current.pop(removable)
        write_private_json(self._queue_path, current)

    def _event_payload(
        self, queued_event: QueuedEvent, install_id: object
    ) -> dict[str, object]:
        properties = queued_event["properties"]
        event_id = queued_event["uuid"]
        return {
            "event": queued_event["event"],
            "timestamp": queued_event["timestamp"],
            "uuid": event_id,
            "properties": {
                "distinct_id": install_id,
                "$insert_id": event_id,
                "$process_person_profile": False,
                "$ip": "0",
                "cli_version": _cli_version(),
                "os_family": _os_family(),
                **properties,
            },
        }

    def _read_state(self) -> tuple[StateKind, dict[str, object]]:
        try:
            value = json.loads(self._state_path.read_text())
        except FileNotFoundError:
            return "missing", {}
        except (OSError, json.JSONDecodeError):
            return "invalid", {}
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("enabled"), bool)
            or not isinstance(value.get("install_id"), str)
            or not value["install_id"]
        ):
            return "invalid", {}
        return "valid", value

    @staticmethod
    def _new_state(enabled: bool) -> dict[str, object]:
        return {"enabled": enabled, "install_id": str(uuid4())}

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
            timestamp = item.get("timestamp")
            event_id = item.get("uuid")
            if (
                not isinstance(event, str)
                or not isinstance(properties, dict)
                or not isinstance(timestamp, str)
                or not isinstance(event_id, str)
                or not self._valid_timestamp(timestamp)
                or not self._valid_uuid(event_id)
            ):
                continue
            normalized = self._normalize_properties(event, properties)
            if normalized is not None:
                sanitized.append(
                    {
                        "event": event,
                        "properties": normalized,
                        "timestamp": timestamp,
                        "uuid": event_id,
                    }
                )
        return sanitized[-MAX_QUEUED_EVENTS:]

    @staticmethod
    def _valid_timestamp(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return datetime.fromisoformat(value).tzinfo is not None
        except ValueError:
            return False

    @staticmethod
    def _valid_uuid(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            UUID(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def _normalize_properties(
        event: str, properties: dict[str, object]
    ) -> dict[str, object] | None:
        if event == "telemetry setup completed":
            bucket = properties.get("duration_bucket")
            return {"duration_bucket": bucket} if bucket in DURATION_BUCKETS else None
        if event == "dashboard opened":
            data_available = properties.get("data_available")
            return (
                {"data_available": data_available}
                if type(data_available) is bool
                else None
            )
        if event in {"first snapshot ready", "telemetry active day"}:
            return {}
        if event == "collector failed" and properties.get("stage") == "snapshot":
            return {"stage": "snapshot"}
        return None


_PENDING_EVENTS: SimpleQueue[tuple[str, dict[str, object]]] = SimpleQueue()
_WORKER_LOCK = Lock()


def _store() -> TrackingStore:
    return TrackingStore(tracking_state_path(), tracking_queue_path())


def _schedule(action: str, properties: dict[str, object]) -> None:
    _PENDING_EVENTS.put((action, properties))
    flush_in_background()


def record_setup_completed(duration_seconds: float, *, default_enabled: bool) -> None:
    if duration_seconds < 1:
        bucket = "under_1_second"
    elif duration_seconds < 5:
        bucket = "under_5_seconds"
    elif duration_seconds < 15:
        bucket = "under_15_seconds"
    else:
        bucket = "15_seconds_or_more"
    try:
        store = _store()
        store.initialize(default_enabled=default_enabled)
        store.record_setup_completed(bucket)
    except Exception:
        return
    flush_in_background()


def record_dashboard_opened(data_available: bool) -> None:
    _schedule("dashboard opened", {"data_available": data_available})
    if data_available:
        _schedule("active day", {"day": date.today().isoformat()})


def record_first_snapshot_ready() -> None:
    _schedule("first snapshot ready", {})


def record_collector_failure() -> None:
    _schedule("collector failure", {"day": date.today().isoformat()})


def set_tracking_enabled(enabled: bool) -> None:
    _store().set_enabled(enabled)


def tracking_status() -> TrackingStatus:
    return _store().status()


def flush_in_background() -> None:
    if not _WORKER_LOCK.acquire(blocking=False):
        return

    def flush() -> None:
        try:
            store = _store()
            while True:
                try:
                    action, properties = _PENDING_EVENTS.get_nowait()
                except Empty:
                    break
                if action == "active day":
                    store.record_active_day(str(properties["day"]))
                elif action == "first snapshot ready":
                    store.record_first_snapshot_ready()
                elif action == "collector failure":
                    store.record_collector_failure(str(properties["day"]))
                else:
                    store.record(action, properties)
            store.send_queued()
        except Exception:
            pass
        finally:
            _WORKER_LOCK.release()
        if not _PENDING_EVENTS.empty():
            flush_in_background()

    try:
        Thread(target=flush, daemon=True).start()
    except Exception:
        _WORKER_LOCK.release()
