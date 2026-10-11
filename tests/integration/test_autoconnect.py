"""Integration test of autoconnect: a background connect hands its report to the session once.

The report, with the mount paths and the remote memory index, is queued by the server and
injected by the next PostToolUse or UserPromptSubmit hook. An in-session `remote_connect`
returns the report as its tool result and queues nothing.

Only `autoconnect.json` in the user's message base may open connections at session start.
A file inside the repository is ignored. A session id change (`/clear`, `/resume`) in the
same process connects the hosts configured for the project that are not connected yet.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import run_hook, wait_for

CONNECT_LIMIT_S = 30.0
QUIET_S = 4.0


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

    #--- disconnect while the remote memory directory still exists ---
    for alias in ('h1', 'h2'):
        text, is_error = sess.mcp.call('remote_disconnect', alias=alias)
        assert not is_error, text
    assert not (sess.rt / 'h1' / 'mem').is_mount()


@pytest.mark.parametrize('name', ['remote-ssh.json', 'remote-ssh.local.json'])
def test_repository_config_never_connects(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path, name: str) -> None:
    cwd = tmp_path / 'cloned'
    (cwd / '.claude').mkdir(parents=True)
    hosts = [{'alias': 'evil', 'host': sshd['host'], 'path': str(remote_dir), 'autoconnect': True}]
    (cwd / '.claude' / name).write_text(json.dumps({'hosts': hosts}))
    sess = session('a', cwd=cwd)

    assert wait_for(lambda: sess.rt.joinpath('owner.json').exists() and json.loads(sess.rt.joinpath('owner.json').read_text()).get('autoconnect_read'), 15)
    time.sleep(QUIET_S)
    text, _ = sess.mcp.call('remote_status')
    assert text.startswith('status · 0 hosts'), text
    assert not (sess.rt / 'evil').exists()
    assert not (sess.rt / 'context' / 'evil.md').exists()
    assert json.loads((sess.rt / 'owner.json').read_text())['shared_config_noted'] == sess.sid


def test_resume_keeps_hosts_and_connects_the_new_sessions_hosts(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    other_dir = (tmp_path / 'remote-other').resolve()
    other_dir.mkdir()
    sess = session('a')
    text, is_error = sess.mcp.call('remote_connect', alias='h0', host=sshd['host'], path=str(other_dir))
    assert not is_error, text

    #--- the user config for this project appears, then the session is resumed into another one ---
    msg_base = tmp_path / 'msg-a'
    (msg_base / 'autoconnect.json').write_text(json.dumps({'projects': {str(sess.cwd): {'hosts': [
        {'alias': 'h0', 'host': sshd['host'], 'path': str(other_dir)},
        {'alias': 'h1', 'host': sshd['host'], 'path': str(remote_dir)}]}}}))
    run_hook(sess.env, 'SessionEnd', {'session_id': sess.sid, 'reason': 'resume'})
    new_sid = 'a2-0000-0000-0000-000000000000'
    run_hook(sess.env, 'SessionStart', {'session_id': new_sid, 'cwd': str(sess.cwd), 'source': 'resume'})

    assert wait_for((sess.rt / 'context' / 'h1.md').exists, CONNECT_LIMIT_S), sess.mcp.call('remote_status')[0]
    text, _ = sess.mcp.call('remote_status')
    assert re.search(r'^h0 ', text, re.M) and re.search(r'^h1 ', text, re.M), text
    assert (sess.rt / 'context' / 'h0.md').exists() is False, 'h0 stayed connected and must not reconnect'
