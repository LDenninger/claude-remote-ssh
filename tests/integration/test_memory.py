"""Integration test I5: remote memory index rewrite and a write through the memory mount."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from remote_ssh import memory


@pytest.fixture
def remote_memory_dir(remote_dir: Path) -> Any:
    """Create the remote memory directory of `remote_dir` under the real HOME, removed afterwards.

    The slug derives from the unique `tmp_path`, so the project directory is new and only it
    is ever removed.
    """
    mem_dir = Path(memory.remote_memory_dir(str(Path.home()), str(remote_dir)))
    project_dir = mem_dir.parent
    assert not project_dir.exists(), f'{project_dir} already exists'
    mem_dir.mkdir(parents=True)
    yield mem_dir
    shutil.rmtree(project_dir)


def test_i5_memory_rewrite_and_write_through_mount(session: Any, sshd: dict, remote_dir: Path, remote_memory_dir: Path) -> None:
    (remote_memory_dir / 'MEMORY.md').write_text('# Memory\n- [Cluster quota](quota.md) is 4 GPUs\n- [Docs](https://example.com/x)\n')
    (remote_memory_dir / 'quota.md').write_text('The quota is 4 GPUs.\n')
    sess = session('a')

    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    mem_mount = sess.rt / 'h1' / 'mem'
    assert f'memory: {mem_mount}' in text
    assert '## Remote memory (h1)' in text
    assert f'[Cluster quota]({mem_mount}/quota.md)' in text
    assert '[Docs](https://example.com/x)' in text

    #--- the rewritten link resolves through the mount, and writes reach the remote ---
    assert os.path.ismount(mem_mount)
    assert (mem_mount / 'quota.md').read_text() == 'The quota is 4 GPUs.\n'
    (mem_mount / 'new_fact.md').write_text('written from the local session\n')
    assert (remote_memory_dir / 'new_fact.md').read_text() == 'written from the local session\n'

    text, is_error = sess.mcp.call('remote_disconnect', alias='h1')
    assert not is_error, text
    assert not os.path.ismount(mem_mount)
