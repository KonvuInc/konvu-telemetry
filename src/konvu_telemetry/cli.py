"""Console entry point for Konvu Telemetry."""

from __future__ import annotations

import argparse
import json
import sys

from . import keymenu
from .collector import (
    HOOK_RUNNERS,
    HOOKS,
    run_backtest_next_ten,
    run_dashboard,
    run_normalize,
    run_once,
    run_serve,
    run_statusline,
)
from .config import DASHBOARD_PORT, package_version
from .installer import service_status, setup, uninstall, uninstall_homebrew_package
from .preferences import (
    CADENCES,
    MAX_CUSTOM_RULE_LENGTH,
    Preferences,
    custom_rule_prompt,
    read_preferences,
    write_preferences,
)
from .tracking import set_tracking_enabled, tracking_status


def run_setup(arguments: argparse.Namespace) -> None:
    print(
        json.dumps(
            setup(max(1, arguments.interval), not arguments.no_browser),
            indent=2,
        )
    )


def run_status(_arguments: argparse.Namespace) -> None:
    print(json.dumps(service_status(), indent=2))


def run_uninstall(_arguments: argparse.Namespace) -> None:
    print(json.dumps(uninstall(), indent=2), flush=True)
    uninstall_homebrew_package()


def run_tracking(arguments: argparse.Namespace) -> None:
    if arguments.state == "on":
        set_tracking_enabled(True)
    elif arguments.state == "off":
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


CADENCE_PROMPT = "How often should Konvu show the usage box inside a turn?"


def _choose_cadence_interactively(
    rule: str = "", jump_percent: float | None = None
) -> None:
    """Pick a cadence with the arrow keys, or from a numbered list without a terminal."""
    options = list(CADENCES.items())
    current = read_preferences()
    current_row = next(
        (i for i, (key, _) in enumerate(options) if key == current["cadence"]), None
    )
    custom_row = next(
        (i for i, (key, _) in enumerate(options) if key == "custom"), None
    )
    if not keymenu.interactive():
        _choose_cadence_from_a_list(options, current_row, rule, jump_percent)
        return
    print(CADENCE_PROMPT)
    print("Arrows to move, type to describe a custom rule, Enter to save.\n")
    picker = keymenu.Picker(
        [label for _, label in options],
        selected=current_row or 0,
        editable_row=custom_row,
        field=rule or (current["custom_rule"] if not rule else rule),
        placeholder="describe your rule",
        current_row=current_row,
        field_limit=MAX_CUSTOM_RULE_LENGTH,
    )
    chosen = picker.run()
    print()
    if chosen is None:
        print("Nothing changed.")
        return
    row, typed = chosen
    cadence = options[row][0]
    _save_cadence(cadence, typed if cadence == "custom" else "", jump_percent)


def _choose_cadence_from_a_list(
    options: list[tuple[str, str]],
    current_row: int | None,
    rule: str,
    jump_percent: float | None,
) -> None:
    """The same choice over a pipe, where single keypresses cannot be read."""
    print(CADENCE_PROMPT + "\n")
    for index, (_, label) in enumerate(options, start=1):
        marker = " (current)" if index - 1 == current_row else ""
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


def run_cadence(arguments: argparse.Namespace) -> None:
    # Validated before either branch, so a bad threshold cannot slip through the
    # interactive path after the non-interactive one has rejected it.
    if arguments.jump_percent is not None and arguments.jump_percent <= 0:
        arguments.parser.error("--jump-percent must be greater than zero")
    if arguments.status:
        if arguments.cadence or arguments.rule or arguments.jump_percent is not None:
            arguments.parser.error(
                "--status only reports the current choice; it cannot set one"
            )
        _print_cadence(read_preferences())
        return
    if arguments.cadence is None:
        # The flags are carried into the menu rather than dropped, so a value the
        # user passed is never ignored while the command reports success.
        _choose_cadence_interactively(arguments.rule, arguments.jump_percent)
        return
    if not _save_cadence(arguments.cadence, arguments.rule, arguments.jump_percent):
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    """Declare every command once, so no command list can drift out of step."""
    parser = argparse.ArgumentParser(
        description="Local-first Claude Code and Codex usage monitoring"
    )
    # Declared before the subcommand so the flag stands alone, which is what
    # anyone checking a version types.
    parser.add_argument(
        "--version",
        action="version",
        version=package_version(),
        help="Print the installed version and exit.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    def register(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.set_defaults(parser=command)
        return command

    install = register("setup", "Install the collector, status line and hooks.")
    install.add_argument("--interval", type=int, default=60)
    install.add_argument("--no-browser", action="store_true")
    install.set_defaults(run=run_setup)

    register("status", "Report whether the collector is running.").set_defaults(
        run=run_status
    )
    register("uninstall", "Remove everything this installed.").set_defaults(
        run=run_uninstall
    )

    tracking = register("telemetry", "Control anonymous product analytics.")
    tracking.add_argument("state", choices=["on", "off", "status"])
    tracking.set_defaults(run=run_tracking)

    cadence = register(
        "cadence", "Choose how often the Konvu usage box is shown inside a turn."
    )
    cadence.add_argument(
        "cadence",
        nargs="?",
        choices=sorted(CADENCES),
        help="Omit to choose from a list.",
    )
    cadence.add_argument("--rule", default="", help="Your rule, with cadence 'custom'.")
    cadence.add_argument(
        "--jump-percent",
        type=float,
        default=None,
        help=(
            "Percent a limit must move before showing usage, with 'usage-jump'. "
            "Must be greater than zero."
        ),
    )
    cadence.add_argument(
        "--status",
        action="store_true",
        help="Print the current choice and exit without changing it.",
    )
    cadence.set_defaults(run=run_cadence)

    register("once", "Collect one snapshot and exit.").set_defaults(run=run_once)

    serve = register("serve", "Run the collector loop and the local dashboard.")
    serve.add_argument("--interval", type=int, default=60)
    serve.add_argument("--port", type=int, default=DASHBOARD_PORT)
    serve.set_defaults(run=run_serve)

    register(
        "statusline", "Print the status line for the current session."
    ).set_defaults(run=run_statusline)
    register("normalize", "Write the normalized event export.").set_defaults(
        run=run_normalize
    )
    register("backtest-next-ten", "Backtest the next-ten forecast.").set_defaults(
        run=run_backtest_next_ten
    )

    dashboard = register("dashboard", "Open the local dashboard in a browser.")
    dashboard.add_argument("--port", type=int, default=DASHBOARD_PORT)
    dashboard.set_defaults(run=run_dashboard)

    for name, run in HOOK_RUNNERS.items():
        register(name, f"Agent hook: {name}.").set_defaults(run=run)

    return parser


def main(arguments: list[str] | None = None) -> None:
    argv = sys.argv[1:] if arguments is None else arguments
    # Every hook is dispatched before argparse, whatever else is on the line: a name
    # this build does not know, or an argument it does not accept, must still exit 0
    # in silence, because a non-zero hook blocks the user's prompt.
    command = argv[0] if argv else ""
    if command.endswith("-hook"):
        hook = HOOKS.get(command)
        if hook is not None:
            hook()
        return
    parsed = build_parser().parse_args(argv)
    parsed.run(parsed)
