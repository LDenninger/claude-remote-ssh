"""Integration test: a call over a dead master must not start a new, unowned master.

The user's ssh config says `ControlMaster auto` and `ControlPersist`. Without
`-o ControlMaster=no` on every non-master ssh call, a `remote_exec` after the master died
turns itself into a persistent master at the plugin's socket.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from conftest import is_running, pids_matching, run_hook, wait_for


def test_exec_after_master_death_leaves_no_new_mux_master(session: Any, sshd: dict, remote_dir: Path) -> None:
    opts = sshd['opts'] + ' -o ControlMaster=auto -o ControlPersist=60'
    sess = session('a', REMOTE_SSH_SSH_OPTS=opts)
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    ctl = sess.rt / 'h1' / 'c'
    masters = pids_matching(f'-M -N -S {ctl}')
    assert len(masters) == 1
    os.kill(masters[0], signal.SIGKILL)
    assert wait_for(lambda: not pids_matching(f'-M -N -S {ctl}'), 5)

    sess.mcp.call('remote_exec', alias='h1', command='true', timeout_s=10)
    time.sleep(1.5)
    assert pids_matching(f'{ctl} [mux]') == []


def test_redirected_bash_timeout_ends_the_remote_process(session: Any, sshd: dict, remote_dir: Path) -> None:
    sess = session('b')
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    text, is_error = sess.mcp.call('remote_mode', alias='h1')
    assert not is_error, text
    hook_env = {key: value for key, value in sess.env.items() if key != 'REMOTE_SSH_SSH_OPTS'}
    payload = {'session_id': sess.sid, 'cwd': str(sess.cwd), 'tool_name': 'Bash',
               'tool_input': {'command': 'echo $$ > sleeper.pid; exec sleep 300', 'timeout': 2000}}
    command = run_hook(hook_env, 'PreToolUse', payload)['hookSpecificOutput']['updatedInput']['command']
    result = subprocess.run(['bash', '-c', command], cwd=sess.cwd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 124, (result.stderr, command)
    pid = int((remote_dir / 'sleeper.pid').read_text())
    assert wait_for(lambda: not is_running(pid), 10)
