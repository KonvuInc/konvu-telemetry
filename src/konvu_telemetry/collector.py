#!/usr/bin/env python3
"""Command-line entry point for local telemetry."""

from __future__ import annotations

import argparse
import sys
import time

from .analytics import (
    backtest_next_ten,
)
from .config import DASHBOARD_PORT
from .display import (
    claude_hook,
    claude_prompt_hook,
    codex_hook,
    codex_prompt_hook,
    statusline,
)
from .exporter import write_normalized_events
from .provider_limits import ProviderLimitPoller, stored_provider_quotas
from .service import open_dashboard, run_local_service, write_health
from .snapshot import build_snapshot, write_snapshot


HOOKS = {
    "claude-hook": claude_hook,
    "claude-prompt-hook": claude_prompt_hook,
    "codex-hook": codex_hook,
    "codex-prompt-hook": codex_prompt_hook,
}


def main(arguments: list[str] | None = None) -> None:
    # Hooks are dispatched before argparse: a name this build does not know must exit 0
    # in silence, because a non-zero hook blocks the user's prompt.
    argv = sys.argv[1:] if arguments is None else arguments
    command = argv[0] if argv else ""
    if command.endswith("-hook"):
        hook = HOOKS.get(command)
        if hook is not None:
            hook()
        return
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
            "claude-prompt-hook",
            "normalize",
            "codex-hook",
            "codex-prompt-hook",
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
        try:
            provider_quotas = ProviderLimitPoller(
                initial_snapshots=stored_provider_quotas()
            ).refresh(now)
        except Exception:
            provider_quotas = {"claude": None, "codex": None}
        write_snapshot(build_snapshot(now, provider_quotas=provider_quotas))
        write_health(now)
    elif args.command == "serve":
        run_local_service(max(1, args.interval), args.port)
    elif args.command == "normalize":
        write_normalized_events()
    elif args.command == "backtest-next-ten":
        backtest_next_ten()
    elif args.command == "dashboard":
        open_dashboard(args.port)
    else:
        statusline()
