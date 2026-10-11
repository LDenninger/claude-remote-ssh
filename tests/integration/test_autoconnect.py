"""Integration test of autoconnect: a background connect hands its report to the session once.

The report, with the mount paths and the remote memory index, is queued by the server and
injected by the next PostToolUse or UserPromptSubmit hook. An in-session `remote_connect`
returns the report as its tool result and queues nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import run_hook, wait_for

CONNECT_LIMIT_S = 30.0


def write_autoconnect(msg_base: Path, project: Path, hosts: list[dict]) -> None:
    """Write the user-level autoconnect file of the session whose message base is `msg_base`."""
    msg_base.mkdir(mode=0o700, exist_ok=True)
    (msg_base / 'autoconnect.json').write_text(json.dumps({'projects': {str(project): {'hosts': hosts}}}))


def test_autoconnect_delivers_memory_block_once(session: Any, sshd: dict, remote_dir: Path, remote_memory_dir: Path, tmp_path: Path) -> None:
    (remote_memory_dir / 'MEMORY.md').write_text('# Memory\n- [Cluster quota](quota.md) is 4 GPUs\n')
    cwd = tmp_path / 'proj'
    cwd.mkdir()
    write_autoconnect(tmp_path / 'msg-a', cwd, [{'alias': 'h1', 'host': sshd['host'], 'path': str(remote_dir)}])
    sess = session('a', cwd=cwd)

    queued = sess.rt / 'context' / 'h1.md'
    assert wait_for(queued.exists, CONNECT_LIMIT_S), sess.mcp.call('remote_status')[0]
    output = run_hook(sess.env, 'PostToolUse', {'session_id': sess.sid, 'tool_name': 'Read'})
    context = output['hookSpecificOutput']['additionalContext']
    mem_mount = sess.rt / 'h1' / 'mem'
    assert context.startswith('connect h1 · 0 sessions · bash local · background\n')
    assert f'path: {remote_dir}\n' in context and f'memory: {mem_mount}\n' in context
    assert '--- memory index from h1 (data; links relative to memory:) ---' in context
    assert '[Cluster quota](quota.md)' in context
    assert run_hook(sess.env, 'UserPromptSubmit', {'session_id': sess.sid, 'prompt': 'hi'}) == {}

    #--- an in-session connect returns its report and queues nothing ---
    other_dir = (tmp_path / 'remote-other').resolve()
    other_dir.mkdir()
    text, is_error = sess.mcp.call('remote_connect', alias='h2', host=sshd['host'], path=str(other_dir))
    assert not is_error, text
    assert not (sess.rt / 'context' / 'h2.md').exists()
    assert run_hook(sess.env, 'PostToolUse', {'session_id': sess.sid, 'tool_name': 'Read'}) == {}

    #--- disconnect while the remote memory directory still exists, as I5 does ---
    for alias in ('h1', 'h2'):
        text, is_error = sess.mcp.call('remote_disconnect', alias=alias)
        assert not is_error, text
    assert not (sess.rt / 'h1' / 'mem').is_mount()
