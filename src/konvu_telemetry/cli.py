"""Console entry point for Konvu Telemetry."""

from __future__ import annotations

import argparse
import json
import sys

from .collector import main as collector_main
from .installer import service_status, setup, uninstall
from .tracking import has_tracking_preference, set_tracking_enabled, tracking_status


def telemetry_consent() -> bool:
    """Ask interactive setup users before enabling product analytics."""
    if not sys.stdin.isatty():
        return False
    try:
        response = input(
            "Help improve Konvu with anonymous product insights? Your prompts, code, "
            "files, and Claude/Codex usage stay on your computer. [y/N] "
        )
    except EOFError:
        return False
    return response.strip().lower() in {"y", "yes"}


def installer_main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Install Konvu's local usage monitor")
    parser.add_argument("command", choices=["setup", "status", "uninstall"])
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--telemetry",
        choices=["on", "off"],
        help="Set anonymous telemetry without an interactive prompt.",
    )
    args = parser.parse_args(arguments)
    if args.command == "setup":
        tracking_enabled = (
            args.telemetry == "on"
            if args.telemetry is not None
            else False
            if has_tracking_preference()
            else telemetry_consent()
        )
        print(
            json.dumps(
                setup(max(1, args.interval), not args.no_browser, tracking_enabled),
                indent=2,
            )
        )
    elif args.command == "status":
        print(json.dumps(service_status(), indent=2))
    else:
        print(json.dumps(uninstall(), indent=2))


def tracking_main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Control anonymous product analytics")
    parser.add_argument("command", choices=["on", "off", "status"])
    args = parser.parse_args(arguments)
    if args.command == "on":
        set_tracking_enabled(True)
    elif args.command == "off":
        set_tracking_enabled(False)
    print(json.dumps({"enabled": tracking_status().enabled}))


def main(arguments: list[str] | None = None) -> None:
    command = sys.argv[1:] if arguments is None else arguments
    if command and command[0] == "telemetry":
        tracking_main(command[1:])
        return
    if command and command[0] in {"setup", "status", "uninstall"}:
        installer_main(command)
        return
    collector_main(command)
