"""Integration test of Bash redirect: the PreToolUse rewrite, executed with bash.

The hook runs without `REMOTE_SSH_SSH_OPTS`, as real hooks never see the MCP server's env,
so the rewritten command only reaches the test sshd through the options the server saved.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from conftest import run_hook


def run_bash(command: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a command line with bash as the Bash tool would."""
    return subprocess.run(['bash', '-c', command], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def test_redirect_runs_on_remote_and_local_prefix_runs_locally(session: Any, sshd: dict, remote_dir: Path) -> None:
    sess = session('a')
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    text, is_error = sess.mcp.call('remote_mode', alias='h1')
    assert not is_error and text.startswith('redirect on'), text

    hook_env = {key: value for key, value in sess.env.items() if key not in ('REMOTE_SSH_SSH_OPTS', 'CLAUDE_PLUGIN_OPTION_REDIRECT_CONFIRM')}
    bash_env = {key: value for key, value in os.environ.items() if key != 'SSH_CONNECTION'}

    def pre_tool_use(command: str) -> dict:
        payload = {'session_id': sess.sid, 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': command, 'description': 'test'}}
        return run_hook(hook_env, 'PreToolUse', payload)['hookSpecificOutput']

    #--- redirected: runs in the remote project path over ssh, exit code passes through ---
    output = pre_tool_use('echo "$SSH_CONNECTION" > where.txt; pwd >> where.txt; exit 7')
    assert output['permissionDecision'] == 'ask'
    assert output['permissionDecisionReason'] == 'remote-ssh: runs on h1'
    assert output['updatedInput']['description'] == 'test'
    result = run_bash(output['updatedInput']['command'], sess.cwd, bash_env)
    assert result.returncode == 7, result.stderr
    ssh_connection, pwd = (remote_dir / 'where.txt').read_text().splitlines()
    assert ssh_connection.split()[-1] == sshd['opts'].split('-p ')[1].split()[0]
    assert pwd == str(remote_dir)
    assert not (sess.cwd / 'where.txt').exists()

    #--- local: prefix runs on this machine in the session cwd ---
    output = pre_tool_use('local: echo "[$SSH_CONNECTION]" > where.txt; pwd >> where.txt')
    assert 'permissionDecision' not in output
    result = run_bash(output['updatedInput']['command'], sess.cwd, bash_env)
    assert result.returncode == 0, result.stderr
    assert (sess.cwd / 'where.txt').read_text().splitlines() == ['[]', str(sess.cwd)]

    #--- redirect off: no rewrite at all ---
    text, is_error = sess.mcp.call('remote_mode', alias=None)
    assert not is_error, text
    payload = {'session_id': sess.sid, 'tool_name': 'Bash', 'tool_input': {'command': 'true'}}
    assert run_hook(hook_env, 'PreToolUse', payload) == {}
