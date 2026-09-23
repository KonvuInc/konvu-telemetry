"""Install and remove the local Konvu background collector and display hooks."""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import site
import subprocess
import sys
import sysconfig
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .service import load_health

LABEL = "com.konvu.telemetry"
PORT = 7824
LAUNCHER_NAME = "konvu-launcher"
CLAUDE_STATUSLINE_NAME = "konvu-claude-statusline"
CLAUDE_STATUSLINE_ORIGINAL_NAME = "konvu-claude-statusline-original"
CLAUDE_STATUSLINE_STATE_NAME = "konvu-claude-statusline-state.json"
CONSOLE_COMMAND = "konvu-telemetry"
HOOK_TIMEOUT_SECONDS = 5
REQUIRED_CONSOLE_COMMANDS = ("claude-prompt-hook", "codex-prompt-hook")


@dataclass(frozen=True)
class FileState:
    path: Path
    contents: bytes | None
    mode: int | None


def telemetry_home() -> Path:
    return Path.home() / ".konvu" / "telemetry"


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launcher_path() -> Path:
    return telemetry_home() / LAUNCHER_NAME


def claude_statusline_path() -> Path:
    return telemetry_home() / CLAUDE_STATUSLINE_NAME


def claude_statusline_original_path() -> Path:
    return telemetry_home() / CLAUDE_STATUSLINE_ORIGINAL_NAME


def claude_statusline_state_path() -> Path:
    return telemetry_home() / CLAUDE_STATUSLINE_STATE_NAME


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def codex_hooks_path() -> Path:
    return Path.home() / ".codex" / "hooks.json"


def console_launcher() -> Path:
    """Locate the installed console script without consulting the current directory."""
    invoked = Path(sys.argv[0])
    candidates: list[Path] = [invoked] if invoked.is_absolute() else []
    if site.USER_BASE is not None:
        candidates.append(Path(site.USER_BASE) / "bin" / CONSOLE_COMMAND)
    scripts = sysconfig.get_path("scripts")
    if scripts is not None:
        candidates.append(Path(scripts) / CONSOLE_COMMAND)
    for candidate in candidates:
        if (
            candidate.name == CONSOLE_COMMAND
            and candidate.is_file()
            and console_launcher_is_compatible(candidate)
        ):
            return candidate
    raise RuntimeError(
        "Could not find a current telemetry launcher; install this version and run "
        "setup through its `konvu-telemetry` executable"
    )


def console_launcher_is_compatible(candidate: Path) -> bool:
    """Reject a stale script that only imports this version through PYTHONPATH."""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    try:
        result = subprocess.run(
            [str(candidate), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and all(
        command in result.stdout for command in REQUIRED_CONSOLE_COMMANDS
    )


def install_launcher() -> Path:
    """Create a private, absolute-path launcher for long-lived integrations."""
    logs = telemetry_home()
    logs.mkdir(parents=True, exist_ok=True)
    logs.chmod(0o700)
    path = launcher_path()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        target = shlex.quote(str(console_launcher()))
        # A hook that fails blocks the user's prompt, so hook output is buffered and
        # only emitted on success; every other subcommand keeps its own stdout and status.
        temporary.write_text(
            "#!/bin/sh\n"
            "unset PYTHONPATH\n"
            f"[ -x {target} ] || exit 0\n"
            'case "$1" in\n'
            "*-hook)\n"
            f'  output=$({target} "$@" 2>/dev/null) || exit 0\n'
            '  [ -n "$output" ] && printf \'%s\\n\' "$output"\n'
            "  exit 0\n"
            "  ;;\n"
            "esac\n"
            f'exec {target} "$@"\n',
            encoding="utf-8",
        )
        temporary.chmod(0o700)
        os.replace(temporary, path)
        path.chmod(0o700)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def collector_command() -> list[str]:
    # The private wrapper uses an absolute console script, so an untrusted cwd cannot shadow us.
    path = launcher_path()
    if not path.is_file():
        raise RuntimeError("Konvu launcher is not installed")
    return [str(path)]


def command_text(*arguments: str) -> str:
    return shlex.join([*collector_command(), *arguments])


def backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = path.with_name(f"{path.name}.konvu-backup-{stamp}")
    shutil.copy2(path, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination


def capture_file(path: Path) -> FileState:
    try:
        return FileState(path, path.read_bytes(), path.stat().st_mode & 0o777)
    except FileNotFoundError:
        return FileState(path, None, None)


def restore_file(state: FileState) -> None:
    if state.contents is None:
        state.path.unlink(missing_ok=True)
        return
    state.path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state.path.with_name(f".{state.path.name}.{os.getpid()}.restore")
    try:
        temporary.write_bytes(state.contents)
        temporary.chmod(state.mode or 0o600)
        os.replace(temporary, state.path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json_object(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return parsed


def write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def is_konvu_command(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        arguments = shlex.split(value)
    except ValueError:
        return False
    if not arguments:
        return False
    return Path(arguments[0]).expanduser() == launcher_path()


def is_konvu_statusline(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        arguments = shlex.split(value)
    except ValueError:
        return False
    if not arguments:
        return False
    command = Path(arguments[0]).expanduser()
    return command in {launcher_path(), claude_statusline_path()}


def is_konvu_hook(value: object, command: str) -> bool:
    """Return whether a command is one specific Konvu hook entry."""
    if not isinstance(value, str) or not is_konvu_command(value):
        return False
    try:
        arguments = shlex.split(value)
    except ValueError:
        return False
    return arguments[1:] == [command]


def write_claude_statusline_wrapper(command: str) -> None:
    """Write private scripts that replay an existing status line before telemetry."""
    original = claude_statusline_original_path()
    wrapper = claude_statusline_path()
    for path, contents in (
        (original, f"#!/bin/sh\n{command}\n"),
        (
            wrapper,
            "#!/bin/sh\n"
            "payload=$(cat)\n"
            f"printf '%s' \"$payload\" | {shlex.quote(str(original))}\n"
            f"printf '%s' \"$payload\" | {command_text('statusline')}\n",
        ),
    ):
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(contents, encoding="utf-8")
            temporary.chmod(0o700)
            os.replace(temporary, path)
            path.chmod(0o700)
        finally:
            temporary.unlink(missing_ok=True)


def install_claude_statusline(
    ensure_launcher: bool = True, create_backup: bool = True
) -> Literal["installed"]:
    if ensure_launcher:
        install_launcher()
    path = claude_settings_path()
    settings = load_json_object(path)
    existing = settings.get("statusLine")
    command = existing.get("command") if isinstance(existing, dict) else None
    if existing is not None and not isinstance(command, str):
        raise ValueError(f"Expected statusLine.command to be a string in {path}")
    if create_backup:
        backup(path)
    statusline: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    statusline.update(
        {
            "type": "command",
            "command": command_text("statusline"),
            "refreshInterval": 60,
        }
    )
    wrapper_command = shlex.quote(str(claude_statusline_path()))
    if command == wrapper_command:
        statusline["command"] = wrapper_command
    elif command is not None and not is_konvu_statusline(command):
        write_claude_statusline_wrapper(command)
        write_json(claude_statusline_state_path(), {"statusLine": existing})
        statusline["command"] = wrapper_command
    else:
        claude_statusline_path().unlink(missing_ok=True)
        claude_statusline_original_path().unlink(missing_ok=True)
        claude_statusline_state_path().unlink(missing_ok=True)
    settings["statusLine"] = statusline
    write_json(path, settings)
    return "installed"


def install_hook(
    path: Path, event: str, command: str, create_backup: bool
) -> Literal["installed", "updated"]:
    """Upsert one Konvu hook command into a provider's hook document."""
    document = load_json_object(path)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"Expected hooks to be an object in {path}")
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        raise ValueError(f"Expected hooks.{event} to be an array in {path}")
    desired = command_text(command)
    for group in groups:
        if not isinstance(group, dict):
            continue
        commands = group.get("hooks")
        if not isinstance(commands, list):
            continue
        for hook in commands:
            if isinstance(hook, dict) and is_konvu_hook(hook.get("command"), command):
                if create_backup:
                    backup(path)
                hook["type"] = "command"
                hook["command"] = desired
                hook["timeout"] = HOOK_TIMEOUT_SECONDS
                write_json(path, document)
                return "updated"
    if create_backup:
        backup(path)
    groups.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": desired,
                    "timeout": HOOK_TIMEOUT_SECONDS,
                }
            ]
        }
    )
    write_json(path, document)
    return "installed"


def install_codex_hook(
    ensure_launcher: bool = True, create_backup: bool = True
) -> Literal["installed", "updated"]:
    if ensure_launcher:
        install_launcher()
    return install_hook(codex_hooks_path(), "Stop", "codex-hook", create_backup)


def install_codex_prompt_hook(
    ensure_launcher: bool = True, create_backup: bool = True
) -> Literal["installed", "updated"]:
    """Register the UserPromptSubmit hook that renders usage inside Codex Desktop replies."""
    if ensure_launcher:
        install_launcher()
    return install_hook(
        codex_hooks_path(), "UserPromptSubmit", "codex-prompt-hook", create_backup
    )


def install_claude_prompt_hook(
    ensure_launcher: bool = True, create_backup: bool = True
) -> Literal["installed", "updated"]:
    """Register the UserPromptSubmit hook that renders usage inside Claude Desktop replies."""
    if ensure_launcher:
        install_launcher()
    return install_hook(
        claude_settings_path(), "UserPromptSubmit", "claude-prompt-hook", create_backup
    )


def launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def stop_launch_agent(strict: bool = True) -> bool:
    if sys.platform != "darwin":
        return True
    path = launch_agent_path()
    domain = launchctl_domain()
    result = subprocess.run(
        ["launchctl", "bootout", domain, str(path)], check=False, capture_output=True
    )
    if result.returncode == 0:
        return True
    loaded = subprocess.run(
        ["launchctl", "print", f"{domain}/{LABEL}"], check=False, capture_output=True
    )
    if strict and loaded.returncode == 0:
        raise RuntimeError(
            f"Could not stop {LABEL}: {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return loaded.returncode != 0


def start_launch_agent() -> None:
    domain = launchctl_domain()
    path = launch_agent_path()
    subprocess.run(
        ["launchctl", "bootstrap", domain, str(path)], check=True, capture_output=True
    )
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"],
        check=True,
        capture_output=True,
    )


def install_launch_agent(interval: int, ensure_launcher: bool = True) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Konvu setup currently supports macOS only")
    if ensure_launcher:
        install_launcher()
    path = launch_agent_path()
    logs = telemetry_home()
    logs.mkdir(parents=True, exist_ok=True)
    logs.chmod(0o700)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            *collector_command(),
            "serve",
            "--interval",
            str(max(1, interval)),
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(logs / "collector.log"),
        "StandardErrorPath": str(logs / "collector.error.log"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(plistlib.dumps(payload))
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    stop_launch_agent()
    start_launch_agent()


def integration_paths() -> tuple[Path, Path]:
    return (claude_settings_path(), codex_hooks_path())


def validate_integrations() -> None:
    for path in integration_paths():
        document = load_json_object(path)
        hooks = document.get("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError(f"Expected hooks to be an object in {path}")
        for event in ("Stop", "UserPromptSubmit"):
            if not isinstance(hooks.get(event, []), list):
                raise ValueError(f"Expected hooks.{event} to be an array in {path}")


def restore_installation(states: list[FileState], restart_service: bool) -> None:
    stopped = stop_launch_agent(strict=False)
    for state in states:
        restore_file(state)
    if restart_service and stopped and sys.platform == "darwin":
        start_launch_agent()


def setup(interval: int, open_browser: bool) -> dict[str, str]:
    if sys.platform != "darwin":
        raise RuntimeError("Konvu setup currently supports macOS only")
    validate_integrations()
    claude_path, codex_path = integration_paths()
    states = [
        capture_file(claude_path),
        capture_file(codex_path),
        capture_file(launcher_path()),
        capture_file(claude_statusline_path()),
        capture_file(claude_statusline_original_path()),
        capture_file(claude_statusline_state_path()),
        capture_file(launch_agent_path()),
    ]
    backups = [
        path for path in (backup(claude_path), backup(codex_path)) if path is not None
    ]
    try:
        install_launcher()
        claude = install_claude_statusline(ensure_launcher=False, create_backup=False)
        # Older versions installed a Claude Stop hook; the status line covers the CLI now.
        claude_stop_removed = remove_claude_stop_hook(create_backup=False)
        claude_prompt = install_claude_prompt_hook(
            ensure_launcher=False, create_backup=False
        )
        codex = install_codex_hook(ensure_launcher=False, create_backup=False)
        codex_prompt = install_codex_prompt_hook(
            ensure_launcher=False, create_backup=False
        )
        dashboard = f"http://127.0.0.1:{PORT}/"
        install_launch_agent(interval, ensure_launcher=False)
    except Exception:
        restore_installation(states, states[-1].contents is not None)
        for path in backups:
            path.unlink(missing_ok=True)
        raise
    if open_browser:
        webbrowser.open(dashboard)
    return {
        "claude_statusline": claude,
        "claude_stop_hook": "removed" if claude_stop_removed else "absent",
        "claude_prompt_hook": claude_prompt,
        "codex_hook": codex,
        "codex_prompt_hook": codex_prompt,
        "desktop_hook_review": (
            "Restart Claude Desktop and Codex Desktop. In Codex, open /hooks and "
            "trust the Konvu Telemetry hooks before they can run."
        ),
        "dashboard": dashboard,
    }


def remove_claude_statusline(create_backup: bool = True) -> bool:
    path = claude_settings_path()
    settings = load_json_object(path)
    statusline = settings.get("statusLine")
    if not isinstance(statusline, dict) or not is_konvu_statusline(
        statusline.get("command")
    ):
        return False
    if create_backup:
        backup(path)
    state_path = claude_statusline_state_path()
    if statusline.get("command") == shlex.quote(str(claude_statusline_path())):
        state = load_json_object(state_path) if state_path.is_file() else {}
        original = state.get("statusLine")
        if isinstance(original, dict):
            settings["statusLine"] = original
        else:
            settings.pop("statusLine", None)
    else:
        settings.pop("statusLine", None)
    write_json(path, settings)
    claude_statusline_path().unlink(missing_ok=True)
    claude_statusline_original_path().unlink(missing_ok=True)
    state_path.unlink(missing_ok=True)
    return True


def remove_hook(path: Path, event: str, command: str, create_backup: bool) -> bool:
    """Remove only Konvu's own hook command from a provider's hook document."""
    document = load_json_object(path)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get(event), list):
        return False
    original = hooks[event]
    kept: list[object] = []
    for group in original:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        commands = group.get("hooks")
        if not isinstance(commands, list):
            kept.append(group)
            continue
        retained = [
            hook
            for hook in commands
            if not (
                isinstance(hook, dict) and is_konvu_hook(hook.get("command"), command)
            )
        ]
        if retained:
            next_group = dict(group)
            next_group["hooks"] = retained
            kept.append(next_group)
    if kept == original:
        return False
    if create_backup:
        backup(path)
    hooks[event] = kept
    write_json(path, document)
    return True


def remove_codex_hook(create_backup: bool = True) -> bool:
    return remove_hook(codex_hooks_path(), "Stop", "codex-hook", create_backup)


def remove_codex_prompt_hook(create_backup: bool = True) -> bool:
    """Remove only Konvu's Codex Desktop UserPromptSubmit hook."""
    return remove_hook(
        codex_hooks_path(), "UserPromptSubmit", "codex-prompt-hook", create_backup
    )


def remove_claude_stop_hook(create_backup: bool = True) -> bool:
    """Remove only Konvu's Claude Stop hook, which older versions installed."""
    return remove_hook(claude_settings_path(), "Stop", "claude-hook", create_backup)


def remove_claude_prompt_hook(create_backup: bool = True) -> bool:
    """Remove only Konvu's Claude Desktop UserPromptSubmit hook."""
    return remove_hook(
        claude_settings_path(), "UserPromptSubmit", "claude-prompt-hook", create_backup
    )


def uninstall() -> dict[str, bool]:
    validate_integrations()
    claude_path, codex_path = integration_paths()
    states = [
        capture_file(claude_path),
        capture_file(codex_path),
        capture_file(launcher_path()),
        capture_file(claude_statusline_path()),
        capture_file(claude_statusline_original_path()),
        capture_file(claude_statusline_state_path()),
        capture_file(launch_agent_path()),
    ]
    backups = [
        path for path in (backup(claude_path), backup(codex_path)) if path is not None
    ]
    try:
        stop_launch_agent()
        result = {
            "claude_statusline": remove_claude_statusline(create_backup=False),
            "claude_stop_hook": remove_claude_stop_hook(create_backup=False),
            "claude_prompt_hook": remove_claude_prompt_hook(create_backup=False),
            "codex_hook": remove_codex_hook(create_backup=False),
            "codex_prompt_hook": remove_codex_prompt_hook(create_backup=False),
        }
        for path in (
            launch_agent_path(),
            launcher_path(),
            claude_statusline_path(),
            claude_statusline_original_path(),
            claude_statusline_state_path(),
        ):
            path.unlink(missing_ok=True)
        return result
    except Exception:
        restore_installation(states, states[-1].contents is not None)
        for path in backups:
            path.unlink(missing_ok=True)
        raise


def service_status() -> dict[str, object]:
    """Report whether the local collector is installed and recently refreshed."""
    installed = launch_agent_path().is_file()
    return {
        "installed": installed,
        "health": load_health(),
        "dashboard": f"http://127.0.0.1:{PORT}/",
    }
