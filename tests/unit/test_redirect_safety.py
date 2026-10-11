"""Unit tests for the redirect safety fixes: deny rules, ControlMaster=no, watchdog, sshfs quoting, remote_exec."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from remote_ssh import hooks, server, state
from remote_ssh.conn import Master, exec_argv
from remote_ssh.mount import MountError, sshfs_argv, ssh_command_option

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> state.Runtime:
    """Return an isolated runtime with host gpu-a as redirect target and an empty Claude config dir."""
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt'))
    monkeypatch.setenv('REMOTE_SSH_MSG_BASE', str(tmp_path / 'msg'))
    monkeypatch.setenv('REMOTE_SSH_CLAUDE_PID', str(os.getpid()))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'home-claude'))
    monkeypatch.delenv('CLAUDE_PLUGIN_OPTION_REDIRECT_CONFIRM', raising=False)
    monkeypatch.delenv('REMOTE_SSH_SSH_OPTS', raising=False)
    rt = state.Runtime.current()

    def apply(saved: dict) -> None:
        saved['hosts']['gpu-a'] = {'host': 'user@gpu', 'path': '/srv/proj', 'slug': '-srv-proj', 'status': 'connected',
                                   'mounted_fs': False, 'mounted_mem': False, 'error': None}
        saved['redirect'] = 'gpu-a'
    rt.update_state(apply)
    return rt


def write_settings(path: Path, deny: list[str]) -> None:
    """Write a settings file with the given deny rules."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'permissions': {'deny': deny}}))


def pre_tool_use(command: str, cwd: Path, **tool_input: Any) -> dict:
    """Run the PreToolUse handler for one Bash call and return its hookSpecificOutput."""
    payload = {'tool_name': 'Bash', 'cwd': str(cwd), 'tool_input': {'command': command, **tool_input}}
    return hooks.dispatch('PreToolUse', payload)['hookSpecificOutput']


#---------------------------------------------------------------------
# Finding 1: deny rules
#---------------------------------------------------------------------

@pytest.mark.parametrize('pattern, command, expected', [
    ('rm -rf:*', 'rm -rf /tmp/x', True),
    ('rm -rf:*', 'rm -rf', True),
    ('rm -rf:*', 'rm -r /tmp/x', False),
    ('rm *', 'rm', True),
    ('ls *', 'lsof', False),
    ('ls*', 'lsof', True),
    ('npm run build', 'npm run build', True),
    ('npm run build', 'npm run build --watch', False),
    ('git push *', 'git push origin main', True),
    ('git * main', 'git push origin main', True),
    ('* --version', 'node --version', True),
    ('*', 'anything', True),
])
def test_rule_matches_follows_permission_syntax(pattern: str, command: str, expected: bool) -> None:
    assert hooks.rule_matches(pattern, command) is expected


@pytest.mark.parametrize('command', [
    'rm -rf /srv/x',
    'echo hi && rm -rf /srv/x',
    'echo hi; rm -rf /srv/x',
    'false || rm -rf /srv/x',
    'cat f | rm -rf /srv/x',
    'echo hi\nrm -rf /srv/x',
    'echo $(rm -rf /srv/x)',
    'echo `rm -rf /srv/x`',
    '(rm -rf /srv/x)',
    'FOO=1 rm -rf /srv/x',
    'timeout 5 rm -rf /srv/x',
    'nohup rm -rf /srv/x &',
    'if true; then rm -rf /srv/x; fi',
    'rm -rf "/srv/x"',
    "r'm' -rf /srv/x",
    '/bin/rm -rf /srv/x',
])
def test_deny_rule_blocks_original_command_in_any_position(runtime: state.Runtime, tmp_path: Path, command: str) -> None:
    write_settings(tmp_path / 'proj' / '.claude' / 'settings.json', ['Bash(rm -rf:*)'])
    output = pre_tool_use(command, tmp_path / 'proj')
    assert output['permissionDecision'] == 'deny'
    assert 'Bash(rm -rf:*)' in output['permissionDecisionReason']
    assert 'updatedInput' not in output


def test_deny_rule_does_not_block_other_commands(runtime: state.Runtime, tmp_path: Path) -> None:
    write_settings(tmp_path / 'proj' / '.claude' / 'settings.json', ['Bash(rm -rf:*)'])
    output = pre_tool_use('echo "rm -r not here"; ls -la', tmp_path / 'proj')
    assert output['permissionDecision'] == 'ask'
    assert 'updatedInput' in output


@pytest.mark.parametrize('where', ['user', 'project', 'local'])
def test_deny_rules_are_read_from_all_three_settings_files(runtime: state.Runtime, tmp_path: Path, where: str) -> None:
    paths = {'user': tmp_path / 'home-claude' / 'settings.json',
             'project': tmp_path / 'proj' / '.claude' / 'settings.json',
             'local': tmp_path / 'proj' / '.claude' / 'settings.local.json'}
    write_settings(paths[where], ['Read(./secrets)', 'Bash(curl *)'])
    output = pre_tool_use('curl http://x', tmp_path / 'proj')
    assert output['permissionDecision'] == 'deny'
    assert str(paths[where]) in output['permissionDecisionReason']


def test_unreadable_settings_deny(runtime: state.Runtime, tmp_path: Path) -> None:
    settings = tmp_path / 'proj' / '.claude' / 'settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text('{not json')
    output = pre_tool_use('ls', tmp_path / 'proj')
    assert output['permissionDecision'] == 'deny'
    assert 'deny rules' in output['permissionDecisionReason']


def test_local_prefix_skips_the_redirect_and_keeps_the_command(runtime: state.Runtime, tmp_path: Path) -> None:
    write_settings(tmp_path / 'proj' / '.claude' / 'settings.json', ['Bash(rm -rf:*)'])
    output = pre_tool_use('local: rm -rf x', tmp_path / 'proj')
    assert output['updatedInput']['command'] == 'rm -rf x'


def test_approval_prompt_shows_original_command_truncated(runtime: state.Runtime, tmp_path: Path) -> None:
    assert pre_tool_use('ls -la', tmp_path)['permissionDecisionReason'] == 'remote-ssh: runs on gpu-a: ls -la'
    long_reason = pre_tool_use('echo ' + 'x' * 1000, tmp_path)['permissionDecisionReason']
    assert long_reason.startswith('remote-ssh: runs on gpu-a: echo xxx')
    assert len(long_reason) <= 335


#---------------------------------------------------------------------
# Finding 2: ControlMaster=no
#---------------------------------------------------------------------

def test_exec_argv_and_ssh_argv_never_become_a_master() -> None:
    assert exec_argv(Path('/c'), 'h', 'ls', None)[3:7] == ['-o', 'BatchMode=yes', '-o', 'ControlMaster=no']
    assert Master('a', 'h', Path('/c'), ['-p', '1']).ssh_argv() == ['ssh', '-S', '/c', '-o', 'BatchMode=yes', '-o', 'ControlMaster=no', '-p', '1']


def test_check_and_stop_pass_control_master_no(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, b'', b''))
    ctl = tmp_path / 'c'
    ctl.touch()
    master = Master('a', 'h', ctl)
    assert master.check()
    master.stop()
    assert [call[call.index('-O') + 1] for call in calls] == ['check', 'exit']
    for call in calls:
        assert ['-o', 'ControlMaster=no'] == call[3:5]


def test_hook_rewrite_never_becomes_a_master(runtime: state.Runtime, tmp_path: Path) -> None:
    argv = shlex.split(pre_tool_use('ls', tmp_path)['updatedInput']['command'])
    assert 'ControlMaster=no' in argv


#---------------------------------------------------------------------
# Finding 3: remote watchdog
#---------------------------------------------------------------------

@pytest.mark.parametrize('given, seconds', [(5000, 5), (5001, 6), (1, 1), (None, 120), ('x', 120), (0, 120), (-5, 120), (True, 120), (9_999_000, 600)])
def test_bash_timeout_s(given: Any, seconds: int) -> None:
    assert hooks.bash_timeout_s({} if given is None else {'timeout': given}) == seconds


def test_redirected_command_carries_the_remote_watchdog(runtime: state.Runtime, tmp_path: Path) -> None:
    remote = shlex.split(pre_tool_use('sleep 99', tmp_path, timeout=7000)['updatedInput']['command'])[-1]
    watchdog = shlex.split(remote)
    assert watchdog[:2] == ['bash', '-c'] and watchdog[3] == '7'
    default = shlex.split(shlex.split(pre_tool_use('sleep 99', tmp_path)['updatedInput']['command'])[-1])
    assert default[3] == '120'


#---------------------------------------------------------------------
# Finding 4: fail closed
#---------------------------------------------------------------------

def test_pre_tool_use_hook_is_blocking_on_failure() -> None:
    hooks_json = json.loads((ROOT / 'hooks' / 'hooks.json').read_text())
    assert hooks_json['hooks']['PreToolUse'][0]['hooks'][0]['onFailure'] == 'block'


def test_flag_without_findable_runtime_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt'))
    monkeypatch.setattr(state.Runtime, 'current', classmethod(lambda cls: None))
    payload = {'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}
    assert hooks.dispatch('PreToolUse', payload) is None
    (tmp_path / 'rt' / 'abc').mkdir(parents=True)
    (tmp_path / 'rt' / 'abc' / 'redirect-on').touch()
    assert hooks.dispatch('PreToolUse', payload)['hookSpecificOutput']['permissionDecision'] == 'deny'


#---------------------------------------------------------------------
# Finding 5: sshfs ssh_command
#---------------------------------------------------------------------

def test_ssh_command_escapes_spaces_and_backslashes_without_quotes() -> None:
    master = Master('a', 'h', Path('/run/my dir/c'), ['-i', '/k/a\\b c'])
    assert ssh_command_option(master) == 'ssh -S /run/my\\ dir/c -o BatchMode=yes -o ControlMaster=no -i /k/a\\\\b\\ c'
    assert "'" not in ssh_command_option(master)


def test_ssh_command_rejects_comma() -> None:
    with pytest.raises(MountError, match='comma'):
        sshfs_argv(Master('a', 'h', Path('/run/c,d')), '/p', Path('/m'))


#---------------------------------------------------------------------
# Finding 6: remote_exec validation and error flag
#---------------------------------------------------------------------

@pytest.fixture
def app_with_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> server.Server:
    """Return a server holding a stub host gpu-a whose exec returns what `outputs` holds."""
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt2'))
    monkeypatch.setenv('REMOTE_SSH_CLAUDE_PID', str(os.getpid()))
    app = server.Server(state.Runtime.current(), server.Config(), store=SimpleNamespace(), on_exit=lambda: None)
    host = SimpleNamespace(alias='gpu-a', status='connected', abs_path='/p', lock=threading.Lock())
    host.outputs = []
    host.master = SimpleNamespace(exec=lambda command, cwd, timeout: host.outputs.pop(0))
    app.hosts['gpu-a'] = host
    app.stub = host
    return app


@pytest.mark.parametrize('bad', [0, -1, 3601, 1.5, '5', True, []])
def test_remote_exec_rejects_bad_timeout(app_with_host: server.Server, bad: Any) -> None:
    text, is_error = app_with_host.call_tool('remote_exec', {'alias': 'gpu-a', 'command': 'x', 'timeout_s': bad})
    assert is_error and 'timeout_s must be an integer from 1 to 3600' in text


@pytest.mark.parametrize('good', [1, 3600, None])
def test_remote_exec_accepts_good_timeout(app_with_host: server.Server, good: Any) -> None:
    app_with_host.stub.outputs = [('', '', 0)]
    args = {'alias': 'gpu-a', 'command': 'x', **({} if good is None else {'timeout_s': good})}
    assert app_with_host.call_tool('remote_exec', args) == ('exit 0 on gpu-a', False)


@pytest.mark.parametrize('code, is_error', [(0, False), (1, False), (2, False), (123, False), (124, True), (137, True), (255, True)])
def test_remote_exec_error_flag_only_for_timeout_signal_or_ssh_failure(app_with_host: server.Server, code: int, is_error: bool) -> None:
    app_with_host.stub.outputs = [('found\n', '', code)]
    text, flag = app_with_host.call_tool('remote_exec', {'alias': 'gpu-a', 'command': 'grep x f'})
    assert flag is is_error
    assert text.startswith(f'exit {code} on gpu-a') and 'found' in text
