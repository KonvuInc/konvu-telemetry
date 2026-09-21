"""Reproducible synthetic collector benchmark."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import tracemalloc

from .live import IncrementalLiveState
from .snapshot import build_snapshot


def session_count(snapshot: dict[str, object]) -> int:
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list):
        raise RuntimeError("snapshot did not contain a session list")
    return len(sessions)


def write_fixture(root: Path, sessions: int, prompts: int, now: datetime) -> int:
    total_bytes = 0
    project = root / "project"
    project.mkdir(parents=True)
    for session_index in range(sessions):
        session_id = f"benchmark-{session_index:04d}"
        rows: list[str] = []
        for prompt_index in range(prompts):
            timestamp = now - timedelta(
                seconds=(sessions - session_index) * prompts + prompts - prompt_index
            )
            iso = timestamp.isoformat().replace("+00:00", "Z")
            rows.append(
                json.dumps(
                    {
                        "timestamp": iso,
                        "sessionId": session_id,
                        "message": {"role": "user", "content": "benchmark prompt"},
                    }
                )
            )
            rows.append(
                json.dumps(
                    {
                        "timestamp": iso,
                        "sessionId": session_id,
                        "effort": "medium",
                        "message": {
                            "id": f"{session_id}-{prompt_index}",
                            "role": "assistant",
                            "model": "claude-sonnet-4-6",
                            "stop_reason": "end_turn",
                            "usage": {
                                "input_tokens": 500,
                                "output_tokens": 100,
                                "cache_creation_input_tokens": 50,
                                "cache_read_input_tokens": 2_000,
                            },
                        },
                    }
                )
            )
        payload = "\n".join(rows) + "\n"
        (project / f"{session_id}.jsonl").write_text(payload, encoding="utf-8")
        total_bytes += len(payload.encode())
    return total_bytes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark synthetic Konvu telemetry collection."
    )
    parser.add_argument("--sessions", type=int, default=50)
    parser.add_argument("--prompts", type=int, default=20)
    parser.add_argument("--max-cold-seconds", type=float)
    parser.add_argument("--max-incremental-seconds", type=float)
    parser.add_argument("--max-peak-mib", type=float)
    arguments = parser.parse_args()
    if arguments.sessions < 1 or arguments.prompts < 1:
        raise SystemExit("sessions and prompts must be positive")

    with tempfile.TemporaryDirectory(prefix="konvu-benchmark-") as directory:
        workspace = Path(directory)
        transcript_root = workspace / "claude"
        now_datetime = datetime.now(timezone.utc)
        corpus_bytes = write_fixture(
            transcript_root, arguments.sessions, arguments.prompts, now_datetime
        )
        os.environ["KONVU_LIVE_USAGE_HOME"] = str(workspace / "state")
        os.environ["KONVU_LIVE_USAGE_CLAUDE_DIR"] = str(transcript_root)
        os.environ["KONVU_LIVE_USAGE_CODEX_DIR"] = str(workspace / "codex")
        state = IncrementalLiveState()
        tracemalloc.start()
        started = time.perf_counter()
        cold = build_snapshot(now_datetime.timestamp(), state)
        cold_seconds = time.perf_counter() - started
        started = time.perf_counter()
        warm = build_snapshot(now_datetime.timestamp(), state)
        warm_seconds = time.perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result = {
            "fixture": {
                "sessions": arguments.sessions,
                "prompts_per_session": arguments.prompts,
                "bytes": corpus_bytes,
            },
            "cold_seconds": round(cold_seconds, 4),
            "incremental_seconds": round(warm_seconds, 4),
            "peak_traced_bytes": peak_bytes,
            "cold_sessions": session_count(cold),
            "incremental_sessions": session_count(warm),
        }
        print(json.dumps(result, indent=2))
        failures: list[str] = []
        if (
            arguments.max_cold_seconds is not None
            and cold_seconds > arguments.max_cold_seconds
        ):
            failures.append("cold collection exceeded its limit")
        if (
            arguments.max_incremental_seconds is not None
            and warm_seconds > arguments.max_incremental_seconds
        ):
            failures.append("incremental collection exceeded its limit")
        if (
            arguments.max_peak_mib is not None
            and peak_bytes > arguments.max_peak_mib * 1024 * 1024
        ):
            failures.append("peak traced allocations exceeded their limit")
        if failures:
            raise SystemExit("; ".join(failures))


if __name__ == "__main__":
    main()
