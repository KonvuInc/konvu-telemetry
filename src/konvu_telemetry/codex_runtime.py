"""Identify the Codex binary that launched the current hook process."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# First Codex release whose TUI renders ANSI colors in hook messages (openai/codex#50359).
CODEX_ANSI_HOOK_VERSION = (0, 162, 0)
# Codex runs hooks through a shell, and the Konvu launcher adds one more hop.
MAX_ANCESTOR_HOPS = 8
VERSION_OUTPUT = re.compile(r"codex-cli (\d+(?:\.\d+)*)\s*")
LIBPROC = "/usr/lib/libproc.dylib"
PROC_PIDPATHINFO_MAXSIZE = 4096


@dataclass(frozen=True)
class AncestorProcess:
    """One process above the hook: its parent, real executable, terminal and start time."""

    ppid: int
    executable: Path
    has_terminal: bool
    started_at: float | None


def run_text(command: list[str], timeout: float) -> str | None:
    """Run a short probe in the C locale and return its stdout, or None when it fails."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            # ps prints its start dates in the user's locale otherwise.
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def linux_process(pid: int, proc: Path) -> AncestorProcess | None:
    """Read a process from /proc, whose exe link names the binary actually running."""
    try:
        # The command name is parenthesized and may contain spaces.
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        executable = Path(os.readlink(proc / "exe"))
    except (OSError, ValueError, IndexError):
        return None
    # A binary replaced while running reads as "codex (deleted)" and never matches.
    return AncestorProcess(int(fields[1]), executable, fields[4] != "0", None)


def macos_executable(pid: int) -> Path | None:
    """The real executable path; `ps` only reports argv[0], which may be a bare name."""
    try:
        libproc = ctypes.CDLL(LIBPROC)
    except OSError:
        return None
    buffer = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
    if libproc.proc_pidpath(pid, buffer, PROC_PIDPATHINFO_MAXSIZE) <= 0:
        return None
    path = Path(os.fsdecode(buffer.value))
    return path if path.is_absolute() else None


def macos_process(pid: int) -> AncestorProcess | None:
    """Read a process from ps plus libproc."""
    output = run_text(["ps", "-o", "ppid=,tty=,lstart=", "-p", str(pid)], timeout=1)
    fields = output.split() if output else []
    executable = macos_executable(pid)
    if len(fields) < 3 or not fields[0].isdigit() or executable is None:
        return None
    try:
        started = datetime.strptime(" ".join(fields[2:]), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return AncestorProcess(
        int(fields[0]), executable, fields[1] not in {"??", "-"}, started.timestamp()
    )


def parent_process(pid: int) -> AncestorProcess | None:
    """Describe one ancestor of the hook process."""
    proc = Path("/proc") / str(pid)
    return linux_process(pid, proc) if proc.is_dir() else macos_process(pid)


def hook_codex_process() -> AncestorProcess | None:
    """Find the nearest ancestor process that is a Codex executable."""
    pid = os.getppid()
    for _ in range(MAX_ANCESTOR_HOPS):
        if pid <= 1:
            return None
        found = parent_process(pid)
        if found is None:
            return None
        if found.executable.name == "codex":
            return found
        pid = found.ppid
    return None


def codex_binary_version(binary: Path) -> tuple[int, ...] | None:
    """Ask a Codex executable for its release version; pre-releases read as unknown."""
    output = run_text([str(binary), "--version"], timeout=2)
    match = VERSION_OUTPUT.fullmatch(output or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def codex_tui_renders_ansi() -> bool:
    """Whether the Codex TUI running this hook shows ANSI colors instead of raw escapes.

    The running binary decides, not the version that created the session, since a resumed
    session may run on another install. Only a Codex attached to a terminal is a TUI: the
    desktop app and editor extensions show the Stop message as plain text. A binary
    replaced after the process started no longer describes it. Unknown fails closed.
    """
    codex = hook_codex_process()
    if codex is None or not codex.has_terminal:
        return False
    try:
        status = codex.executable.stat()
    except OSError:
        return False
    # mv and tar keep a file's build-time mtime; ctime still records when it landed here.
    modified = max(status.st_mtime, status.st_ctime)
    # ps reports whole seconds, so allow the second the process started in.
    if codex.started_at is not None and modified > codex.started_at + 1:
        return False
    version = codex_binary_version(codex.executable)
    return version is not None and version >= CODEX_ANSI_HOOK_VERSION
