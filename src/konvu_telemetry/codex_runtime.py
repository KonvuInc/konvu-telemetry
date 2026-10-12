"""Decide whether the Codex TUI running the current hook can render ANSI colors."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
from pathlib import Path

# First Codex release whose TUI renders ANSI colors in hook messages (openai/codex#50359).
CODEX_ANSI_HOOK_VERSION = (0, 162, 0)
VERSION_OUTPUT = re.compile(r"codex-cli (\d+(?:\.\d+)*)\s*")
# Codex runs hooks through a shell, and the Konvu launcher adds a hop or two.
MAX_ANCESTOR_HOPS = 8


def run_text(command: list[str]) -> str | None:
    """Run a short probe and return its stdout, or None when it fails."""
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=2
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def process_info(
    pid: int, proc_root: Path = Path("/proc")
) -> tuple[int, Path, bool] | None:
    """A process's parent pid, real executable, and whether it has a terminal."""
    proc = proc_root / str(pid)
    if proc.is_dir():
        try:
            # The command name is parenthesized and may contain spaces.
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            return int(fields[1]), Path(os.readlink(proc / "exe")), fields[4] != "0"
        except (OSError, ValueError, IndexError):
            return None
    # macOS: ps reports argv[0], which may be a bare name, so libproc gives the real path.
    fields = (run_text(["ps", "-o", "ppid=,tty=", "-p", str(pid)]) or "").split()
    buffer = ctypes.create_string_buffer(4096)
    try:
        found = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidpath(pid, buffer, 4096)
    except OSError:
        return None
    if len(fields) != 2 or not fields[0].isdigit() or found <= 0:
        return None
    return int(fields[0]), Path(os.fsdecode(buffer.value)), fields[1] != "??"


def codex_tui_renders_ansi() -> bool:
    """Whether the Codex TUI running this hook shows ANSI colors instead of raw escapes.

    The running binary decides, since a resumed session may run on another install than
    the one that created it. Only a Codex with a terminal is a TUI: the desktop app and
    editor extensions show the Stop message as plain text. Unknown fails closed.
    """
    pid = os.getppid()
    for _ in range(MAX_ANCESTOR_HOPS):
        info = process_info(pid) if pid > 1 else None
        if info is None:
            return False
        pid, executable, has_terminal = info
        if executable.name == "codex":
            output = run_text([str(executable), "--version"]) if has_terminal else None
            match = VERSION_OUTPUT.fullmatch(output or "")
            version = tuple(int(part) for part in match[1].split(".")) if match else ()
            return version >= CODEX_ANSI_HOOK_VERSION
    return False
