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


def _save_cadence(
    cadence: str, rule: str = "", jump_percent: float | None = None
) -> bool:
    """Store one choice, reporting a refusal or an unwritable home as a message.

    Every write goes through here so no surface can reach the user as a traceback.
    """
    try:
        preference = write_preferences(cadence, rule, jump_percent)
    except ValueError as error:
        print(f"Nothing changed: {error}.")
        return False
    except OSError as error:
        print(f"Could not save your choice: {error}.")
        return False
    _print_cadence(preference)
    if preference["cadence"] == "custom":
        # A custom rule needs code, so hand over the prompt that writes it.
        print("\nGive this to your coding agent:\n")
        print(custom_rule_prompt(preference["custom_rule"]))
    return True


def _choose_cadence_interactively(
    rule: str = "", jump_percent: float | None = None
) -> None:
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
    try:
        # Parsed rather than pre-matched: str.isdigit() accepts digits int() rejects.
        choice = int(answer)
    except ValueError:
        print("Not a listed choice; nothing changed.")
        return
    if not 1 <= choice <= len(options):
        print("Not a listed choice; nothing changed.")
        return
    cadence = options[choice - 1][0]
    if cadence != "custom":
        _save_cadence(cadence, jump_percent=jump_percent)
        return
    if not rule:
        try:
            rule = input("Describe your rule: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
    _save_cadence("custom", rule, jump_percent)


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
        help=(
            "Percent a limit must move before showing usage, with 'usage-jump'. "
            "Must be greater than zero."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the current choice and exit without changing it.",
    )
    args = parser.parse_args(arguments)
    # Validated before either branch, so a bad threshold cannot slip through the
    # interactive path after the non-interactive one has rejected it.
    if args.jump_percent is not None and args.jump_percent <= 0:
        parser.error("--jump-percent must be greater than zero")
    if args.status:
        if args.cadence or args.rule or args.jump_percent is not None:
            parser.error("--status only reports the current choice; it cannot set one")
        _print_cadence(read_preferences())
        return
    if args.cadence is None:
        # The flags are carried into the menu rather than dropped, so a value the
        # user passed is never ignored while the command reports success.
        _choose_cadence_interactively(args.rule, args.jump_percent)
        return
    if not _save_cadence(args.cadence, args.rule, args.jump_percent):
        raise SystemExit(2)


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
