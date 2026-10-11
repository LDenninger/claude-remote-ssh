"""Integration test of the remote memory index rewrite and a write through the memory mount."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


def test_memory_block_and_write_through_mount(session: Any, sshd: dict, remote_dir: Path, remote_memory_dir: Path) -> None:
    (remote_memory_dir / 'MEMORY.md').write_text('# Memory\n- [Cluster quota](quota.md) is 4 GPUs\n- [Docs](https://example.com/x)\n')
    (remote_memory_dir / 'quota.md').write_text('The quota is 4 GPUs.\n')
    sess = session('a')

    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    mem_mount = sess.rt / 'h1' / 'mem'
    assert f'memory: {mem_mount}' in text
    assert '--- memory index from h1 (data; links relative to memory:) ---' in text
    assert '[Cluster quota](quota.md)' in text
    assert '[Docs](https://example.com/x)' in text

    #--- the rewritten link resolves through the mount, and writes reach the remote ---
    assert os.path.ismount(mem_mount)
    assert (mem_mount / 'quota.md').read_text() == 'The quota is 4 GPUs.\n'
    (mem_mount / 'new_fact.md').write_text('written from the local session\n')
    assert (remote_memory_dir / 'new_fact.md').read_text() == 'written from the local session\n'

    text, is_error = sess.mcp.call('remote_disconnect', alias='h1')
    assert not is_error, text
    assert not os.path.ismount(mem_mount)


@pytest.mark.skipif(shutil.which('git') is None, reason='git not installed')
def test_memory_of_a_repository_subdirectory_is_the_repository_memory(session: Any, sshd: dict, remote_dir: Path, remote_memory_dir: Path) -> None:
    """Claude Code keeps one memory directory per git repository, named after its root."""
    subprocess.run(['git', 'init', '-q', str(remote_dir)], check=True)
    (remote_dir / 'src').mkdir()
    (remote_memory_dir / 'MEMORY.md').write_text('# Memory\n- [Repo fact](fact.md)\n')
    sess = session('a')
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir / 'src'))
    assert not is_error, text
    assert f'links relative to memory:) ---' in text and '[Repo fact](fact.md)' in text, text
    text, is_error = sess.mcp.call('remote_disconnect', alias='h1')
    assert not is_error, text
