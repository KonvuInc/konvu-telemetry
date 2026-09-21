"""Console entry point for Konvu Telemetry."""

from __future__ import annotations

import argparse
import json
import sys

from .collector import main as collector_main
from .installer import service_status, setup, uninstall


def installer_main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Install Konvu's local usage monitor")
    parser.add_argument("command", choices=["setup", "status", "uninstall"])
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(arguments)
    if args.command == "setup":
        print(json.dumps(setup(max(1, args.interval), not args.no_browser), indent=2))
    elif args.command == "status":
        print(json.dumps(service_status(), indent=2))
    else:
        print(json.dumps(uninstall(), indent=2))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"setup", "status", "uninstall"}:
        installer_main(sys.argv[1:])
        return
    collector_main()
