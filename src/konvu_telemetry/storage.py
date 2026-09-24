"""Local paths, private JSON storage, and transcript discovery."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import json
import os
from pathlib import Path
from threading import Lock
from typing import Callable, Iterator, TypeVar, cast

from .config import ALLOWED_PROVIDERS, FILE_CACHE_LIMIT, SESSION_ID_PATTERN

_FILE_CACHE: dict[tuple[str, str], tuple[int, float, object]] = {}
_WRITE_LOCK = Lock()
Result = TypeVar("Result")
Item = TypeVar("Item")


def file_cached(func: Callable[[Path], Result]) -> Callable[[Path], Result]:
    """Memoise a whole-file parse against the file's size and modification time."""

    @wraps(func)
    def wrapper(file_path: Path) -> Result:
        try:
            stat = file_path.stat()
        except OSError:
            return func(file_path)
        key = (func.__name__, str(file_path))
        cached = _FILE_CACHE.get(key)
        if (
            cached is not None
            and cached[0] == stat.st_size
            and cached[1] == stat.st_mtime
        ):
            return cast(Result, cached[2])
        value = func(file_path)
        _FILE_CACHE[key] = (stat.st_size, stat.st_mtime, value)
        if len(_FILE_CACHE) > FILE_CACHE_LIMIT:
            oldest = next(iter(_FILE_CACHE))
            _FILE_CACHE.pop(oldest, None)
        return value

    return wrapper


def file_cached_list(
    func: Callable[[Path], Iterator[Item]],
) -> Callable[[Path], list[Item]]:
    """Materialize and cache a file-backed iterator."""

    @file_cached
    def collect(file_path: Path) -> list[Item]:
        return list(func(file_path))

    return collect


def home_dir() -> Path:
    configured = os.environ.get("KONVU_LIVE_USAGE_HOME")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".konvu" / "telemetry"
    )


def valid_session_id(session_id: object) -> bool:
    """Return whether a session identifier is safe for local filenames and HTML attributes."""
    return (
        isinstance(session_id, str)
        and SESSION_ID_PATTERN.fullmatch(session_id) is not None
    )


def ensure_private_directory(path: Path) -> None:
    """Create a user-only state directory without weakening its permissions."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def write_private_json(path: Path, value: object) -> None:
    """Atomically write local telemetry state with user-only permissions."""
    payload = json.dumps(value, separators=(",", ":")).encode()
    ensure_private_directory(path.parent)
    with _WRITE_LOCK:
        _write_private_bytes(path, payload)


def write_private_json_if_changed(path: Path, value: object) -> bool:
    """Atomically write JSON only when its serialized contents changed."""
    payload = json.dumps(value, separators=(",", ":")).encode()
    ensure_private_directory(path.parent)
    with _WRITE_LOCK:
        try:
            if path.read_bytes() == payload:
                return False
        except OSError:
            pass
        _write_private_bytes(path, payload)
    return True


def _write_private_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def pinned_path() -> Path:
    return home_dir() / "pinned.json"


def pinned_sessions() -> list[dict[str, object]]:
    """Sessions kept on the dashboard past the live window, for testing against real history."""
    try:
        rows = json.loads(pinned_path().read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and valid_session_id(row.get("id"))
        and isinstance(row.get("provider"), str)
        and row["provider"] in ALLOWED_PROVIDERS
        and SESSION_ID_PATTERN.fullmatch(row["id"])
    ]


def snapshot_path() -> Path:
    return home_dir() / "live-sessions.json"


def normalized_events_path() -> Path:
    return home_dir() / "normalized-events.json"


def baseline_path() -> Path:
    return home_dir() / "baselines.json"


def health_path() -> Path:
    return home_dir() / "health.json"


def notification_state_path() -> Path:
    return home_dir() / "notification-state.json"


def quota_attribution_path() -> Path:
    return home_dir() / "quota-attribution.json"


def tracking_state_path() -> Path:
    return home_dir() / "tracking-state.json"


def tracking_queue_path() -> Path:
    return home_dir() / "tracking-queue.json"


def claude_quota_path() -> Path:
    return home_dir() / "claude-quotas.json"


def session_path(provider: str, session_id: str) -> Path:
    if provider not in ALLOWED_PROVIDERS or not valid_session_id(session_id):
        raise ValueError("Invalid local telemetry session identity")
    return home_dir() / "sessions" / f"{provider}-{session_id}.json"


def claude_roots() -> list[Path]:
    configured = os.environ.get("KONVU_LIVE_USAGE_CLAUDE_DIR")
    if configured:
        return [
            Path(part).expanduser() for part in configured.split(os.pathsep) if part
        ]
    return [Path.home() / ".claude" / "projects"]


def codex_roots() -> list[Path]:
    configured = os.environ.get("KONVU_LIVE_USAGE_CODEX_DIR")
    if configured:
        return [
            Path(part).expanduser() for part in configured.split(os.pathsep) if part
        ]
    return [Path.home() / ".codex" / "sessions"]


def transcript_files(root: Path) -> Iterator[Path]:
    """Yield JSONL files physically contained by a configured transcript root."""
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return
    for candidate in root.rglob("*.jsonl"):
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            candidate.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        yield candidate


def root_claude_transcripts(root: Path) -> Iterator[Path]:
    """Yield direct Claude session transcripts within a configured root."""
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return
    for path in transcript_files(root):
        try:
            if path.parent.resolve(strict=True).parent == resolved_root:
                yield path
        except OSError:
            continue


def as_number(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def parse_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
