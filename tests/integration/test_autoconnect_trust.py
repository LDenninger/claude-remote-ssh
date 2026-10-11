"""Integration test of the autoconnect trust boundary and of autoconnect after /resume.

Only `autoconnect.json` in the user's message base may open connections at session start.
A file inside the repository is ignored. A session id change (`/clear`, `/resume`) in the
same process connects the hosts configured for the project that are not connected yet.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import run_hook, wait_for

CONNECT_LIMIT_S = 30.0
QUIET_S = 4.0


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
    assert text.startswith('no hosts connected'), text
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
    assert 'h0' in text and 'h1' in text, text
    assert (sess.rt / 'context' / 'h0.md').exists() is False, 'h0 stayed connected and must not reconnect'
