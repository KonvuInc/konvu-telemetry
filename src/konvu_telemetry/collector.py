"""Handlers for the collector commands, dispatched by `cli`."""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable

from .analytics import (
    backtest_next_ten,
)
from .display import (
    claude_hook,
    claude_prompt_hook,
    codex_hook,
    codex_prompt_hook,
    statusline,
)
from .exporter import write_normalized_events
from .provider_limits import (
    ProviderLimitPoller,
    stored_provider_quotas,
    write_provider_quotas,
)
from .service import (
    collector_process_lock,
    open_dashboard,
    run_local_service,
    write_health,
)
from .snapshot import build_snapshot, write_snapshot


HOOKS = {
    "claude-hook": claude_hook,
    "claude-prompt-hook": claude_prompt_hook,
    "codex-hook": codex_hook,
    "codex-prompt-hook": codex_prompt_hook,
}


def _hook_runner(hook: Callable[[], None]) -> Callable[[argparse.Namespace], None]:
    """Adapt a hook, which takes nothing, to the shape every command handler has."""

    def run(_arguments: argparse.Namespace) -> None:
        hook()

    return run


HOOK_RUNNERS: dict[str, Callable[[argparse.Namespace], None]] = {
    name: _hook_runner(hook) for name, hook in HOOKS.items()
}


def run_once(_arguments: argparse.Namespace) -> None:
    with collector_process_lock():
        now = time.time()
        try:
            provider_quotas = ProviderLimitPoller(
                initial_snapshots=stored_provider_quotas()
            ).refresh(now)
        except Exception:
            provider_quotas = stored_provider_quotas()
        write_provider_quotas(provider_quotas)
        write_snapshot(build_snapshot(now, provider_quotas=provider_quotas))
        write_health(now)


def run_serve(arguments: argparse.Namespace) -> None:
    run_local_service(max(1, arguments.interval), arguments.port)


def run_normalize(_arguments: argparse.Namespace) -> None:
    write_normalized_events()


def run_backtest_next_ten(_arguments: argparse.Namespace) -> None:
    backtest_next_ten()


def run_dashboard(arguments: argparse.Namespace) -> None:
    open_dashboard(arguments.port)


def run_statusline(_arguments: argparse.Namespace) -> None:
    statusline()
