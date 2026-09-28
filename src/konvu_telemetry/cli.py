"""Console entry point for Konvu Telemetry."""

from __future__ import annotations

import argparse
import json
import sys

from .collector import main as collector_main
from .installer import service_status, setup, uninstall, uninstall_homebrew_package
from .preferences import (
    CADENCES,
    Preferences,
    custom_rule_prompt,
    read_preferences,
    write_preferences,
)
from .tracking import set_tracking_enabled, tracking_status


def installer_main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Install Konvu's local usage monitor")
    parser.add_argument("command", choices=["setup", "status", "uninstall"])
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(arguments)
    if args.command == "setup":
        print(
            json.dumps(
                setup(max(1, args.interval), not args.no_browser),
                indent=2,
            )
        )
    elif args.command == "status":
        print(json.dumps(service_status(), indent=2))
    else:
        print(json.dumps(uninstall(), indent=2), flush=True)
        uninstall_homebrew_package()


def tracking_main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Control anonymous product analytics")
    parser.add_argument("command", choices=["on", "off", "status"])
    args = parser.parse_args(arguments)
    if args.command == "on":
        set_tracking_enabled(True)
    elif args.command == "off":
        set_tracking_enabled(False)
    print(json.dumps({"enabled": tracking_status().enabled}))


def _print_cadence(preference: Preferences) -> None:
    print(
        json.dumps(
            {
                "cadence": preference["cadence"],
                "label": CADENCES[preference["cadence"]],
                "custom_rule": preference["custom_rule"],
                "jump_percent": preference["jump_percent"],
            },
            indent=2,
        )
    )


def _choose_cadence_interactively() -> None:
    """Offer the same options the dashboard shows, numbered for the terminal."""
    options = list(CADENCES.items())
    current = read_preferences()
    print("How often should Konvu show the usage box inside a turn?\n")
    for index, (key, label) in enumerate(options, start=1):
        marker = " (current)" if key == current["cadence"] else ""
        print(f"  {index}. {label}{marker}")
    print()
    try:
        answer = input("Choose 1-%d: " % len(options)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not answer.isdigit() or not 1 <= int(answer) <= len(options):
        print("Not a listed choice; nothing changed.")
        return
    cadence = options[int(answer) - 1][0]
    if cadence != "custom":
        _print_cadence(write_preferences(cadence))
        return
    try:
        rule = input("Describe your rule: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    preference = write_preferences("custom", rule)
    # A custom rule needs code, so hand over the prompt that writes it.
    print("\nGive this to your coding agent:\n")
    print(custom_rule_prompt(preference["custom_rule"]))


def cadence_main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(
        description="Choose how often the Konvu usage box is shown inside a turn"
    )
    parser.add_argument(
        "cadence",
        nargs="?",
        choices=sorted(CADENCES),
        help="Omit to choose from a list.",
    )
    parser.add_argument("--rule", default="", help="Your rule, with cadence 'custom'.")
    parser.add_argument(
        "--jump-percent",
        type=float,
        default=None,
        help="Percent a limit must move before showing usage, with 'usage-jump'.",
    )
    parser.add_argument(
        "--status", action="store_true", help="Print the current choice."
    )
    args = parser.parse_args(arguments)
    if args.status:
        _print_cadence(read_preferences())
        return
    if args.cadence is None:
        _choose_cadence_interactively()
        return
    preference = write_preferences(args.cadence, args.rule, args.jump_percent)
    _print_cadence(preference)
    if preference["cadence"] == "custom":
        print("\nGive this to your coding agent:\n")
        print(custom_rule_prompt(preference["custom_rule"]))


def main(arguments: list[str] | None = None) -> None:
    command = sys.argv[1:] if arguments is None else arguments
    if command and command[0] == "cadence":
        cadence_main(command[1:])
        return
    if command and command[0] == "telemetry":
        tracking_main(command[1:])
        return
    if command and command[0] in {"setup", "status", "uninstall"}:
        installer_main(command)
        return
    collector_main(command)
