"""Minimal, anonymous product analytics for Konvu Telemetry."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import platform
from queue import Empty, SimpleQueue
from threading import Lock, Thread, Timer
import time
from typing import Callable, Iterator, Literal, TypedDict
from urllib.request import Request
from uuid import UUID, uuid4

from .config import package_version
from .outbound import open_without_redirects
from .preferences import CADENCES
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
SETUP_FAILURE_STAGES = frozenset({"integrations", "install"})
AGENT_PROVIDERS = frozenset({"claude", "codex"})
DASHBOARD_ACTIONS = frozenset(
    {"compact prompt copied", "session inspector opened", "notifications enabled"}
)
# Old per-event day markers, honoured so the upgrade day sends no second copy.
LEGACY_DAY_KEYS = {
    "telemetry active day": "active_day",
    "collector failed": "collector_failure_day",
}
SUPPRESSING_VARIABLES = ("DO_NOT_TRACK", "CI")


def analytics_suppressed() -> bool:
    """Return whether DO_NOT_TRACK or a CI environment turns analytics off."""
    return any(
        os.environ.get(name, "").strip().lower() not in {"", "0", "false"}
        for name in SUPPRESSING_VARIABLES
    )


def _send_to_posthog(payload: bytes) -> None:
    if analytics_suppressed():
        raise OSError("Product analytics are suppressed in this environment")
    request = Request(
        POSTHOG_BATCH_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # A redirect raises here as an HTTPError, so the batch never follows it.
    with open_without_redirects(request, DELIVERY_TIMEOUT_SECONDS) as response:
        if not 200 <= response.status < 300:
            raise OSError("PostHog rejected telemetry batch")


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

    def record_daily(self, event: str, properties: dict[str, object], day: str) -> None:
        """Queue an event at most once per day for each distinct property set."""
        normalized = self._normalize_properties(event, properties)
        if normalized is None:
            return
        key = event + json.dumps(normalized, sort_keys=True)
        with self._locked():
            kind, state = self._read_state()
            if kind != "valid" or not bool(state["enabled"]):
                return
            sent = state.get("daily_sent")
            sent = (
                {
                    name: value
                    for name, value in sent.items()
                    if isinstance(value, str) and value >= day
                }
                if isinstance(sent, dict)
                else {}
            )
            legacy_key = LEGACY_DAY_KEYS.get(event)
            if sent.get(key) == day or (
                legacy_key is not None and state.get(legacy_key) == day
            ):
                return
            self._append(event, normalized)
            state = dict(state)
            sent[key] = day
            state["daily_sent"] = sent
            write_private_json(self._state_path, state)

    def send_queued(self) -> float | None:
        with self._locked():
            kind, state = self._read_state()
            queue = self._sanitized_queue()
            next_send_at = state.get("next_send_at")
            if kind != "valid" or not bool(state["enabled"]) or not queue:
                return None
            if (
                isinstance(next_send_at, (int, float))
                and not isinstance(next_send_at, bool)
                and self._clock() < next_send_at
            ):
                return next_send_at - self._clock()
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
                return self._record_delivery_failure(state)
            current = self._sanitized_queue()
            if current[: len(queue)] != queue:
                return None
            state = dict(state)
            if any(item["event"] == "telemetry setup completed" for item in queue):
                state["setup_delivered"] = True
            if any(item["event"] == "first snapshot ready" for item in queue):
                state["first_snapshot_delivered"] = True
            state.pop("delivery_failures", None)
            state.pop("next_send_at", None)
            write_private_json(self._state_path, state)
            write_private_json(self._queue_path, current[len(queue) :])
            return None

    def _record_delivery_failure(self, state: dict[str, object]) -> float:
        previous = state.get("delivery_failures")
        failures = (
            previous + 1
            if isinstance(previous, int) and not isinstance(previous, bool)
            else 1
        )
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
        return float(delay)

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
                "cli_version": package_version(),
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
            or not self._valid_uuid(value["install_id"])
        ):
            return "invalid", {}
        if value.get("first_snapshot_ready") is True:
            value = dict(value)
            value["first_snapshot_delivered"] = True
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
        migrated = False
        for item in value:
            if not isinstance(item, dict):
                continue
            event = item.get("event")
            properties = item.get("properties")
            timestamp = item.get("timestamp")
            event_id = item.get("uuid")
            if not isinstance(event, str) or not isinstance(properties, dict):
                continue
            if timestamp is None:
                timestamp = datetime.fromtimestamp(
                    self._clock(), timezone.utc
                ).isoformat()
                migrated = True
            elif not isinstance(timestamp, str) or not self._valid_timestamp(timestamp):
                continue
            if event_id is None:
                event_id = str(uuid4())
                migrated = True
            elif not isinstance(event_id, str) or not self._valid_uuid(event_id):
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
        bounded = sanitized[-MAX_QUEUED_EVENTS:]
        if migrated:
            write_private_json(self._queue_path, bounded)
        return bounded

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
            return (
                {"duration_bucket": bucket}
                if isinstance(bucket, str) and bucket in DURATION_BUCKETS
                else None
            )
        if event == "dashboard opened":
            data_available = properties.get("data_available")
            return (
                {"data_available": data_available}
                if type(data_available) is bool
                else None
            )
        if event == "telemetry setup failed":
            stage = properties.get("stage")
            return (
                {"stage": stage}
                if isinstance(stage, str) and stage in SETUP_FAILURE_STAGES
                else None
            )
        if event in {"first snapshot ready", "telemetry active day"}:
            return {}
        if event == "collector failed" and properties.get("stage") == "snapshot":
            return {"stage": "snapshot"}
        if event == "compact prompt copied":
            provider = properties.get("provider")
            return (
                {"provider": provider}
                if isinstance(provider, str) and provider in AGENT_PROVIDERS
                else None
            )
        if event in {"session inspector opened", "notifications enabled"}:
            return {}
        if event == "settings saved":
            cadence = properties.get("cadence")
            analysis = properties.get("context_analysis_enabled")
            if (
                not isinstance(cadence, str)
                or cadence not in CADENCES
                or type(analysis) is not bool
            ):
                return None
            return {"cadence": cadence, "context_analysis_enabled": analysis}
        return None


_PENDING_EVENTS: SimpleQueue[tuple[str, dict[str, object], str | None]] = SimpleQueue()
_WORKER_LOCK = Lock()
_RETRY_LOCK = Lock()
_RETRY_TIMER: Timer | None = None


def _store() -> TrackingStore:
    return TrackingStore(tracking_state_path(), tracking_queue_path())


def _schedule(event: str, properties: dict[str, object], daily: bool = True) -> None:
    if analytics_suppressed():
        return
    _PENDING_EVENTS.put(
        (event, properties, date.today().isoformat() if daily else None)
    )
    flush_in_background()


def _duration_bucket(duration_seconds: float) -> str:
    if duration_seconds < 1:
        return "under_1_second"
    if duration_seconds < 5:
        return "under_5_seconds"
    if duration_seconds < 15:
        return "under_15_seconds"
    return "15_seconds_or_more"


def store_suppression_opt_out() -> bool:
    """Store DO_NOT_TRACK or CI as an opt-out, since the launchd collector never sees it."""
    if not analytics_suppressed():
        return False
    try:
        _store().set_enabled(False)
    except Exception:
        pass
    return True


def record_setup_completed(duration_seconds: float, *, default_enabled: bool) -> None:
    if store_suppression_opt_out():
        return
    try:
        store = _store()
        store.initialize(default_enabled=default_enabled)
        store.record_setup_completed(_duration_bucket(duration_seconds))
    except Exception:
        return
    flush_in_background()


def record_setup_failed(stage: str, *, default_enabled: bool) -> None:
    """Queue and try once to send a failed setup, since no resident process may follow."""
    if store_suppression_opt_out():
        return
    try:
        store = _store()
        store.initialize(default_enabled=default_enabled)
        store.record("telemetry setup failed", {"stage": stage})
        store.send_queued()
    except Exception:
        return


def record_dashboard_opened(data_available: bool) -> None:
    _schedule("dashboard opened", {"data_available": data_available})


def record_active_day() -> None:
    _schedule("telemetry active day", {})


def record_first_snapshot_ready() -> None:
    _schedule("first snapshot ready", {}, daily=False)


def record_collector_failure() -> None:
    _schedule("collector failed", {"stage": "snapshot"})


def record_dashboard_action(event: str, properties: dict[str, object]) -> bool:
    """Queue an allowlisted dashboard click; return whether the event is known."""
    if event not in DASHBOARD_ACTIONS:
        return False
    _schedule(event, properties)
    return True


def record_settings_saved(cadence: str, context_analysis_enabled: bool) -> None:
    _schedule(
        "settings saved",
        {"cadence": cadence, "context_analysis_enabled": context_analysis_enabled},
    )


def set_tracking_enabled(enabled: bool) -> None:
    _store().set_enabled(enabled)


def tracking_status() -> TrackingStatus:
    return _store().status()


def _schedule_retry(delay: float) -> None:
    global _RETRY_TIMER

    def retry() -> None:
        global _RETRY_TIMER
        with _RETRY_LOCK:
            _RETRY_TIMER = None
        flush_in_background()

    with _RETRY_LOCK:
        if _RETRY_TIMER is not None and _RETRY_TIMER.is_alive():
            return
        timer = Timer(max(0.0, delay), retry)
        timer.daemon = True
        _RETRY_TIMER = timer
        try:
            timer.start()
        except Exception:
            _RETRY_TIMER = None


def flush_in_background() -> None:
    if not _WORKER_LOCK.acquire(blocking=False):
        return

    def flush() -> None:
        try:
            store = _store()
            while True:
                try:
                    event, properties, day = _PENDING_EVENTS.get_nowait()
                except Empty:
                    break
                if event == "first snapshot ready":
                    store.record_first_snapshot_ready()
                elif day is not None:
                    store.record_daily(event, properties, day)
                else:
                    store.record(event, properties)
            retry_delay = store.send_queued()
            if retry_delay is not None:
                _schedule_retry(retry_delay)
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
