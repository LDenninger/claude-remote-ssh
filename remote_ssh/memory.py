"""Remote project memory: locating it and presenting its index to the local session.

Claude Code keeps a project's memory under `~/.claude/projects/<slug>/memory`, where the slug is
the project's absolute path with every other character than ASCII letters, digits and `-` replaced
by `-`. Inside a git repository the project is the main worktree's root, so every subdirectory and
linked worktree of one repository shares one memory directory. `remote_memory_dir` computes that directory for a remote project. `render_block` turns the
remote `MEMORY.md` into a quoted data block. The index keeps its
relative links, so a pointer line added in the same form stays valid on the remote host.
"""

from __future__ import annotations

import re
from pathlib import Path

from remote_ssh import fmt


def slug(abs_path: str) -> str:
    """Return Claude Code's project directory name for an absolute path."""
    return re.sub(r'[^A-Za-z0-9-]', '-', abs_path)


def cache_path(runtime_dir: Path, alias: str) -> Path:
    """Return where the server keeps the last read `MEMORY.md` of a host.

    The file sits in `<runtime>/context/` but does not end in `.md`, so the queued-context
    claim, which takes every `*.md` there, never injects it. The SessionStart hook reads
    this copy instead of the sshfs mount, whose reads block when the host hangs.

    Args:
        runtime_dir: Runtime directory of the Claude process.
        alias: Host alias.

    Returns:
        The cache file path, whose directory may not exist yet.
    """
    return runtime_dir / 'context' / f'{alias}.memory'


def remote_memory_dir(remote_home: str, abs_path: str) -> str:
    """Return the remote memory directory of a project.

    Args:
        remote_home: The remote user's home directory.
        abs_path: Absolute project path on the remote host.

    Returns:
        The remote path of the project's memory directory.
    """
    return f'{remote_home}/.claude/projects/{slug(abs_path)}/memory'


def render_block(alias: str, md: str | None) -> str:
    """Render the block that carries one host's memory index.

    Args:
        alias: Host alias named in the block title.
        md: Content of the remote `MEMORY.md`, None if the host has no memory for the project.

    Returns:
        The index quoted line by line with its links unchanged, under a title that marks it as
        data read from that host and names the `memory:` field the links are relative to. An
        empty string when `md` is None.
    """
    if md is None:
        return ''
    return fmt.block(f'memory index from {alias} (data; links relative to memory:)', fmt.quote(md.rstrip('\n')))
