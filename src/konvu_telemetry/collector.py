#!/usr/bin/env python3
"""Command-line entry point for local telemetry."""

from __future__ import annotations

import argparse
import time

from .analytics import (
    backtest_next_ten,
)
from .config import DASHBOARD_PORT
from .display import claude_hook, codex_hook, statusline
from .exporter import write_normalized_events
from .service import open_dashboard, run_local_service, write_health
from .snapshot import build_snapshot, write_snapshot


def main(arguments: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Local-first Claude Code and Codex usage monitoring"
    )
    parser.add_argument(
        "command",
        choices=[
            "setup",
            "status",
            "uninstall",
            "once",
            "serve",
            "statusline",
            "claude-hook",
            "normalize",
            "codex-hook",
            "backtest-next-ten",
            "dashboard",
            "telemetry",
        ],
    )
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--port", type=int, default=DASHBOARD_PORT)
    args = parser.parse_args(arguments)
    if args.command == "once":
        now = time.time()
        write_snapshot(build_snapshot(now))
        write_health(now)
    elif args.command == "serve":
        run_local_service(max(1, args.interval), args.port)
    elif args.command == "normalize":
        write_normalized_events()
    elif args.command == "codex-hook":
        codex_hook()
    elif args.command == "claude-hook":
        claude_hook()
    elif args.command == "backtest-next-ten":
        backtest_next_ten()
    elif args.command == "dashboard":
        open_dashboard(args.port)
    else:
        statusline()
