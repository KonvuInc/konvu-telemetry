"""Identify the Codex binary that launched the current hook process."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# First Codex release whose TUI renders ANSI colors in hook messages (openai/codex#50359).
CODEX_ANSI_HOOK_VERSION = (0, 162, 0)
# Codex runs hooks through a shell, and the Konvu launcher adds one more hop.
MAX_ANCESTOR_HOPS = 8
VERSION_OUTPUT = re.compile(r"codex-cli (\d+(?:\.\d+)*)\s*")


def parent_process(pid: int) -> tuple[int, Path] | None:
    """Return a process's parent pid and its own executable path."""
    proc = Path("/proc") / str(pid)
    if proc.is_dir():
        try:
            stat = (proc / "stat").read_text()
            # The command name is parenthesized and may contain spaces.
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            return ppid, Path(os.readlink(proc / "exe"))
        except (OSError, ValueError, IndexError):
            return None
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    ppid, _, command = result.stdout.strip().partition(" ")
    if result.returncode != 0 or not ppid.isdigit() or not command:
        return None
    return int(ppid), Path(command.strip())


def hook_codex_binary() -> Path | None:
    """Find the nearest ancestor process that is a Codex executable."""
    pid = os.getppid()
    for _ in range(MAX_ANCESTOR_HOPS):
        if pid <= 1:
            return None
        found = parent_process(pid)
        if found is None:
            return None
        ppid, executable = found
        if executable.name == "codex":
            return executable
        pid = ppid
    return None


def codex_binary_version(binary: Path) -> tuple[int, ...] | None:
    """Ask a Codex executable for its release version; pre-releases read as unknown."""
    try:
        result = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = VERSION_OUTPUT.fullmatch(result.stdout)
    if result.returncode != 0 or match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def codex_tui_renders_ansi() -> bool:
    """Whether the Codex TUI running this hook shows ANSI colors instead of raw escapes.

    The running binary decides, not the version that created the session, since a resumed
    session may run on another install. The desktop app's bundled CLI shows the Stop
    message as plain text in its hook dialog, so it never qualifies. Unknown fails closed.
    """
    binary = hook_codex_binary()
    if binary is None or ".app/" in binary.as_posix():
        return False
    version = codex_binary_version(binary)
    return version is not None and version >= CODEX_ANSI_HOOK_VERSION
