"""Identify the Claude Code process that owns this plugin process.

Every plugin process (MCP server, hook, monitor) is a descendant of one Claude Code process.
`find_claude` walks the parent chain to it and returns a `ClaudeProc`, whose `key` names the
runtime directory shared by all of that session's plugin processes. The process start token
guards against pid reuse, so `alive` reports False once the owner has exited.
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_WALK_STEPS = 32


@dataclass(frozen=True)
class ClaudeProc:
    """A Claude Code process, identified by pid and start token.

    Attributes:
        pid: Process id.
        start: Opaque start token from `proc_start`, used to detect pid reuse.
    """

    pid: int
    start: str

    @property
    def key(self) -> str:
        """Eight hex characters that name this process's runtime directory."""
        return hashlib.sha1(f'{self.pid}:{self.start}'.encode()).hexdigest()[:8]


#---------------------------------------------------------------------
# Process inspection
#---------------------------------------------------------------------

def _read_stat_fields(pid: int) -> list[str] | None:
    """Return the fields of /proc/<pid>/stat from field 3 (state) onward, or None if gone."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return None
    return stat[stat.rfind(')') + 2:].split()


def _ps(pid: int, fmt: str) -> str | None:
    """Return the stripped `ps -o <fmt> -p <pid>` output, or None if the process is gone."""
    import subprocess  # macOS only: Linux hooks read /proc and skip this import

    try:
        result = subprocess.run(['ps', '-o', fmt, '-p', str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


def proc_start(pid: int) -> str | None:
    """Return an opaque token for the start time of a process.

    Args:
        pid: Process id.

    Returns:
        The start token, equal across calls for the same process and different for a reused
        pid, or None if the process does not exist.
    """
    if sys.platform == 'darwin':
        return _ps(pid, 'lstart=')
    fields = _read_stat_fields(pid)
    if fields is None or len(fields) < 20:
        return None
    return fields[19]  # field 22: starttime


def parent_of(pid: int) -> int | None:
    """Return the parent pid of a process, or None if the process does not exist."""
    if sys.platform == 'darwin':
        output = _ps(pid, 'ppid=')
        return int(output) if output and output.isdigit() else None
    fields = _read_stat_fields(pid)
    if fields is None or len(fields) < 2:
        return None
    return int(fields[1])  # field 4: ppid


def _looks_like_claude(comm: str, exe: str, argv: list[str]) -> bool:
    """Return whether a process's name, executable path or argv identify Claude Code."""
    if Path(comm).name == 'claude' or '/claude/versions/' in exe:
        return True
    if argv and Path(argv[0]).name == 'claude':
        return True
    return any('@anthropic-ai/claude-code' in arg for arg in argv)


def is_claude(pid: int) -> bool:
    """Return whether a process is a Claude Code process.

    Args:
        pid: Process id.

    Returns:
        True if the command name is `claude`, the executable lies under a `/claude/versions/`
        directory, argv[0] is named `claude`, or an argument names the
        `@anthropic-ai/claude-code` package. False otherwise, including for a missing process.
    """
    if sys.platform == 'darwin':
        comm = _ps(pid, 'comm=') or ''
        args = _ps(pid, 'args=') or ''
        return _looks_like_claude(comm, comm, args.split())
    proc_dir = Path(f'/proc/{pid}')
    try:
        comm = (proc_dir / 'comm').read_text().strip()
        argv = [arg for arg in (proc_dir / 'cmdline').read_text(errors='replace').split('\0') if arg]
    except OSError:
        return False
    try:
        exe = os.readlink(proc_dir / 'exe')
    except OSError:
        exe = ''
    return _looks_like_claude(comm, exe, argv)


#---------------------------------------------------------------------
# Owner lookup
#---------------------------------------------------------------------

def find_claude(start_pid: int | None = None) -> ClaudeProc | None:
    """Find the Claude Code process that owns the current process.

    The environment variable `REMOTE_SSH_CLAUDE_PID` overrides the search when set to an
    integer.

    Args:
        start_pid: Process to start the upward walk at, inclusive. Defaults to the parent of
            the current process.

    Returns:
        The nearest Claude Code ancestor, or None if the walk ends without one within 32 steps
        or the overriding pid does not exist.
    """
    override = os.environ.get('REMOTE_SSH_CLAUDE_PID', '').strip()
    if override.isdigit():
        pid = int(override)
        start = proc_start(pid)
        return ClaudeProc(pid, start) if start is not None else None

    pid = os.getppid() if start_pid is None else start_pid
    for _ in range(MAX_WALK_STEPS):
        if pid is None or pid <= 1:
            return None
        if is_claude(pid):
            start = proc_start(pid)
            return ClaudeProc(pid, start) if start is not None else None
        pid = parent_of(pid)
    return None


def alive(proc: ClaudeProc) -> bool:
    """Return whether the process still runs, false after exit even if its pid was reused."""
    return proc_start(proc.pid) == proc.start
