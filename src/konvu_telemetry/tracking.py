"""Minimal, anonymous product analytics for Konvu Telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
from threading import Lock, Thread
from typing import Callable
from urllib.request import Request, urlopen
from uuid import uuid4

from .storage import tracking_queue_path, tracking_state_path, write_private_json

Sender = Callable[[bytes], None]

POSTHOG_PROJECT_KEY = "phc_AKTThCdGvkAkitNvu5cBfoWUbM5gfaBBF67UYNbvj8do"
POSTHOG_BATCH_URL = "https://us.i.posthog.com/batch/"
DELIVERY_TIMEOUT_SECONDS = 0.5
ALLOWED_EVENT_PROPERTIES: dict[str, frozenset[str]] = {
    "telemetry setup completed": frozenset({"duration_bucket"}),
    "dashboard opened": frozenset({"data_available"}),
    "telemetry active day": frozenset(),
    "collector failed": frozenset({"stage"}),
}


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
    """Persist local tracking preference and a small delivery queue."""

    def __init__(
        self,
        state_path: Path,
        queue_path: Path,
        sender: Sender = _send_to_posthog,
    ) -> None:
        self._state_path = state_path
        self._queue_path = queue_path
        self._sender = sender
        self._lock = Lock()

    def status(self) -> TrackingStatus:
        return TrackingStatus(enabled=bool(self._state()["enabled"]))

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            state = self._state()
            state["enabled"] = enabled
            write_private_json(self._state_path, state)
            if not enabled:
                write_private_json(self._queue_path, [])

    def record(self, event: str, properties: dict[str, object]) -> None:
        if event not in ALLOWED_EVENT_PROPERTIES:
            return
        with self._lock:
            if not self.status().enabled:
                return
            queue = self._queue()
            queue.append({"event": event, "properties": properties})
            write_private_json(self._queue_path, queue)

    def send_queued(self) -> None:
        with self._lock:
            queue = self._queue()
            state = self._state()
        if not queue or not bool(state["enabled"]):
            return
        batch = [self._event_payload(item, state["install_id"]) for item in queue]
        try:
            self._sender(
                json.dumps({"api_key": POSTHOG_PROJECT_KEY, "batch": batch}).encode()
            )
        except Exception:
            return
        with self._lock:
            current = self._queue()
            if current[: len(queue)] == queue:
                write_private_json(self._queue_path, current[len(queue) :])

    def _event_payload(
        self, queued_event: dict[str, object], install_id: object
    ) -> dict[str, object]:
        event = str(queued_event["event"])
        queued_properties = queued_event.get("properties")
        properties = (
            queued_properties if isinstance(queued_properties, dict) else {}
        )
        allowed = ALLOWED_EVENT_PROPERTIES[event]
        return {
            "event": event,
            "properties": {
                "distinct_id": install_id,
                "$process_person_profile": False,
                "$ip": "0",
                "cli_version": _cli_version(),
                "os_family": _os_family(),
                **{key: properties[key] for key in allowed if key in properties},
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

    def _queue(self) -> list[dict[str, object]]:
        try:
            value = json.loads(self._queue_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


_STORE = TrackingStore(tracking_state_path(), tracking_queue_path())
_FLUSH_LOCK = Lock()


def _record(event: str, properties: dict[str, object]) -> None:
    try:
        _STORE.record(event, properties)
        flush_in_background()
    except Exception:
        return


def record_setup_completed(duration_seconds: float) -> None:
    if duration_seconds < 1:
        bucket = "under_1_second"
    elif duration_seconds < 5:
        bucket = "under_5_seconds"
    elif duration_seconds < 15:
        bucket = "under_15_seconds"
    else:
        bucket = "15_seconds_or_more"
    _record("telemetry setup completed", {"duration_bucket": bucket})


def record_dashboard_opened(data_available: bool) -> None:
    _record("dashboard opened", {"data_available": data_available})


def record_collector_failure() -> None:
    _record("collector failed", {"stage": "snapshot"})


def flush_in_background() -> None:
    if not _FLUSH_LOCK.acquire(blocking=False):
        return

    def flush() -> None:
        try:
            _STORE.send_queued()
        finally:
            _FLUSH_LOCK.release()

    Thread(target=flush, daemon=True).start()
