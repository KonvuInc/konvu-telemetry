"""Minimal, anonymous product analytics for Konvu Telemetry."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .storage import write_private_json

Sender = Callable[[bytes], None]


@dataclass(frozen=True)
class TrackingStatus:
    enabled: bool


class TrackingStore:
    """Persist local tracking preference and a small delivery queue."""

    def __init__(self, state_path: Path, queue_path: Path, sender: Sender) -> None:
        self._state_path = state_path
        self._queue_path = queue_path
        self._sender = sender

    def status(self) -> TrackingStatus:
        return TrackingStatus(enabled=bool(self._state()["enabled"]))

    def set_enabled(self, enabled: bool) -> None:
        state = self._state()
        state["enabled"] = enabled
        write_private_json(self._state_path, state)
        if not enabled:
            write_private_json(self._queue_path, [])

    def record(self, event: str, properties: dict[str, object]) -> None:
        if not self.status().enabled:
            return
        queue = self._queue()
        queue.append({"event": event, "properties": properties})
        write_private_json(self._queue_path, queue)

    def _state(self) -> dict[str, object]:
        try:
            value = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
            value = {"enabled": True, "install_id": str(uuid4())}
            write_private_json(self._state_path, value)
        return value

    def _queue(self) -> list[dict[str, object]]:
        try:
            value = json.loads(self._queue_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
