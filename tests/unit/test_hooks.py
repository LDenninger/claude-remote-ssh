"""Unit tests for remote_ssh.hooks: redirect rewriting, SessionStart and message injection."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from remote_ssh import hooks, relay, state


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> state.Runtime:
    """Return an isolated runtime owned by the test process."""
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt'))
    monkeypatch.setenv('REMOTE_SSH_MSG_BASE', str(tmp_path / 'msg'))
    monkeypatch.setenv('REMOTE_SSH_CLAUDE_PID', str(os.getpid()))
    monkeypatch.delenv('CLAUDE_PLUGIN_OPTION_REDIRECT_CONFIRM', raising=False)
    monkeypatch.delenv('REMOTE_SSH_SSH_OPTS', raising=False)
    return state.Runtime.current()


def set_redirect(runtime: state.Runtime, status: str = 'connected') -> None:
    """Record host gpu-a in state.json and make it the redirect target."""
    def apply(saved: dict) -> None:
        saved['hosts']['gpu-a'] = {'host': 'user@gpu', 'path': '/srv/proj', 'slug': '-srv-proj', 'status': status,
                                   'mounted_fs': False, 'mounted_mem': False, 'error': None}
        saved['redirect'] = 'gpu-a'
    runtime.update_state(apply)


BASH_INPUT = {'tool_name': 'Bash', 'tool_input': {'command': 'ls -la', 'description': 'List files', 'timeout': 5000}}


#---------------------------------------------------------------------
# PreToolUse
#---------------------------------------------------------------------

def test_pre_tool_use_without_redirect_has_no_output(runtime: state.Runtime) -> None:
    assert hooks.dispatch('PreToolUse', BASH_INPUT) is None


def test_pre_tool_use_with_disconnected_target_has_no_output(runtime: state.Runtime) -> None:
    set_redirect(runtime, status='reconnecting')
    assert hooks.dispatch('PreToolUse', BASH_INPUT) is None


def test_pre_tool_use_ignores_other_tools(runtime: state.Runtime) -> None:
    set_redirect(runtime)
    assert hooks.dispatch('PreToolUse', {'tool_name': 'Read', 'tool_input': {'file_path': '/x'}}) is None


def test_pre_tool_use_redirect_rewrites_and_asks(runtime: state.Runtime) -> None:
    set_redirect(runtime)
    output = hooks.dispatch('PreToolUse', BASH_INPUT)['hookSpecificOutput']
    assert output['hookEventName'] == 'PreToolUse'
    assert output['permissionDecision'] == 'ask'
    assert 'gpu-a' in output['permissionDecisionReason']
    updated = output['updatedInput']
    assert updated['description'] == 'List files'
    assert updated['timeout'] == 5000
    argv = shlex.split(updated['command'])
    assert argv[:3] == ['ssh', '-S', str(runtime.ctl_path('gpu-a'))]
    assert 'user@gpu' in argv
    assert 'cd /srv/proj && ls -la' in argv[-1]


@pytest.mark.parametrize('value', ['false', 'False', '0'])
def test_pre_tool_use_without_confirm_sets_no_decision(runtime: state.Runtime, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv('CLAUDE_PLUGIN_OPTION_REDIRECT_CONFIRM', value)
    set_redirect(runtime)
    output = hooks.dispatch('PreToolUse', BASH_INPUT)['hookSpecificOutput']
    assert 'permissionDecision' not in output
    assert shlex.split(output['updatedInput']['command'])[0] == 'ssh'


def test_pre_tool_use_uses_ssh_opts_saved_by_server(runtime: state.Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('REMOTE_SSH_SSH_OPTS', '-p 1')
    set_redirect(runtime)
    runtime.update_state(lambda saved: saved.update(ssh_opts=['-p', '2222', '-i', '/k']))
    argv = shlex.split(hooks.dispatch('PreToolUse', BASH_INPUT)['hookSpecificOutput']['updatedInput']['command'])
    assert argv[argv.index('-T') - 4:argv.index('-T')] == ['-p', '2222', '-i', '/k']
    assert '-p' not in argv[argv.index('-T'):]
    assert '1' not in argv


def test_pre_tool_use_falls_back_to_env_ssh_opts(runtime: state.Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('REMOTE_SSH_SSH_OPTS', '-p 2222')
    set_redirect(runtime)
    argv = shlex.split(hooks.dispatch('PreToolUse', BASH_INPUT)['hookSpecificOutput']['updatedInput']['command'])
    assert argv[argv.index('-T') - 2:argv.index('-T')] == ['-p', '2222']


def test_pre_tool_use_local_prefix_is_stripped(runtime: state.Runtime) -> None:
    set_redirect(runtime)
    payload = {'tool_name': 'Bash', 'tool_input': {'command': '  local:  git status', 'description': 'Status'}}
    output = hooks.dispatch('PreToolUse', payload)['hookSpecificOutput']
    assert output['updatedInput'] == {'command': 'git status', 'description': 'Status'}
    assert 'permissionDecision' not in output


#---------------------------------------------------------------------
# SessionStart
#---------------------------------------------------------------------

def test_session_start_writes_owner_and_autoconnect(runtime: state.Runtime, tmp_path: Path) -> None:
    project_dir = tmp_path / 'proj'
    (project_dir / '.claude').mkdir(parents=True)
    hosts = [
        {'alias': 'gpu-a', 'host': 'gpu-a', 'path': '~/proj', 'autoconnect': True},
        {'alias': 'gpu-b', 'host': 'gpu-b', 'path': '~/proj', 'autoconnect': False},
    ]
    (project_dir / '.claude' / 'remote-ssh.json').write_text(json.dumps({'hosts': hosts}))
    output = hooks.dispatch('SessionStart', {'session_id': 'abcd1234-ef', 'cwd': str(project_dir), 'source': 'startup'})

    owner = runtime.owner()
    assert owner['session_id'] == 'abcd1234-ef'
    assert owner['cwd'] == str(project_dir)
    assert owner['claude_pid'] == os.getpid()
    assert owner['claude_proc_start'] == runtime.claude.start
    assert owner['name'] == 'proj-abcd'
    assert state.read_json(runtime.dir / 'autoconnect.json') == [{'alias': 'gpu-a', 'host': 'gpu-a', 'path': '~/proj'}]
    assert 'gpu-a' in output['hookSpecificOutput']['additionalContext']
    assert 'gpu-b' not in output['hookSpecificOutput']['additionalContext']


def test_session_start_without_config_writes_empty_autoconnect(runtime: state.Runtime, tmp_path: Path) -> None:
    hooks.dispatch('SessionStart', {'session_id': 'abcd1234', 'cwd': str(tmp_path)})
    assert state.read_json(runtime.dir / 'autoconnect.json') == []


#---------------------------------------------------------------------
# UserPromptSubmit and PostToolUse
#---------------------------------------------------------------------

def test_user_prompt_submit_injects_message_once(runtime: state.Runtime) -> None:
    store = relay.Store()
    store.deliver(relay.new_message('sender-0001', 'other-send', 'gpu-a', 'recv-0001', 'results are ready'))
    output = hooks.dispatch('UserPromptSubmit', {'session_id': 'recv-0001', 'prompt': 'hi'})
    specific = output['hookSpecificOutput']
    assert specific['hookEventName'] == 'UserPromptSubmit'
    assert 'results are ready' in specific['additionalContext']
    assert hooks.dispatch('UserPromptSubmit', {'session_id': 'recv-0001', 'prompt': 'hi'}) is None


def test_post_tool_use_without_messages_has_no_output(runtime: state.Runtime) -> None:
    assert hooks.dispatch('PostToolUse', {'session_id': 'recv-0001', 'tool_name': 'Bash'}) is None


def test_post_tool_use_injects_queued_context_once_with_messages(runtime: state.Runtime) -> None:
    runtime.queue_context('gpu-a', 'remote-ssh: background connect\nconnected gpu-a')
    relay.Store().deliver(relay.new_message('sender-0001', 'other-send', 'gpu-a', 'recv-0001', 'results are ready'))
    context = hooks.dispatch('PostToolUse', {'session_id': 'recv-0001'})['hookSpecificOutput']['additionalContext']
    assert context.index('connected gpu-a') < context.index('results are ready')
    assert hooks.dispatch('PostToolUse', {'session_id': 'recv-0001'}) is None


#---------------------------------------------------------------------
# Shell fast path in hooks/hooks.json
#---------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_root(tmp_path: Path) -> Path:
    """Return a plugin root whose hook.py only reports that Python ran, and for which event."""
    (tmp_path / 'plugin' / 'scripts').mkdir(parents=True)
    (tmp_path / 'plugin' / 'scripts' / 'hook.py').write_text('import sys\nsys.stdin.read()\nprint("python", sys.argv[1])\n')
    return tmp_path / 'plugin'


def run_hook_command(event: str, fake_root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the hooks.json command of `event` as Claude Code does, with a 1 MB stdin payload.

    The payload exceeds a pipe buffer, so a command that exits without reading stdin makes
    the write fail with BrokenPipeError, which Claude Code reports as a hook error.
    """
    [entry] = json.loads((ROOT / 'hooks' / 'hooks.json').read_text())['hooks'][event]
    command = entry['hooks'][0]['command'].replace('${CLAUDE_PLUGIN_ROOT}', str(fake_root))
    payload = json.dumps({'session_id': 'recv-0001', 'tool_response': 'x' * 1_000_000})
    proc = subprocess.Popen(['sh', '-c', command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    proc.stdin.write(payload)
    proc.stdin.close()
    stdout, stderr = proc.stdout.read(), proc.stderr.read()
    return subprocess.CompletedProcess(proc.args, proc.wait(timeout=10), stdout, stderr)


@pytest.mark.parametrize('event', ['UserPromptSubmit', 'PostToolUse'])
def test_message_hooks_start_python_only_for_waiting_work(runtime: state.Runtime, fake_root: Path, event: str) -> None:
    env = dict(os.environ)
    assert run_hook_command(event, fake_root, env).stdout == ''

    #--- a delivered (read/) message is no work ---
    msg = relay.new_message('sender-0001', 'other-send', 'gpu-a', 'recv-0001', 'hi')
    relay.Store().deliver(msg)
    relay.Store().claim('recv-0001', msg['id'])
    result = run_hook_command(event, fake_root, env)
    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')

    relay.Store().deliver(relay.new_message('sender-0001', 'other-send', 'gpu-a', 'recv-0001', 'hi again'))
    assert run_hook_command(event, fake_root, env).stdout == f'python {event}\n'
    relay.Store().claim('recv-0001', relay.Store().pending('recv-0001')[0]['id'])
    assert run_hook_command(event, fake_root, env).stdout == ''

    runtime.queue_context('gpu-a', 'block')
    assert run_hook_command(event, fake_root, env).stdout == f'python {event}\n'


def test_pre_tool_use_hook_starts_python_only_while_redirect_is_set(runtime: state.Runtime, fake_root: Path) -> None:
    env = dict(os.environ)
    assert run_hook_command('PreToolUse', fake_root, env).stdout == ''
    set_redirect(runtime)
    assert run_hook_command('PreToolUse', fake_root, env).stdout == 'python PreToolUse\n'
    runtime.update_state(lambda saved: saved.update(redirect=None))
    assert run_hook_command('PreToolUse', fake_root, env).stdout == ''


def test_default_bases_in_hook_commands_match_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell defaults glob /tmp/remote-ssh-* and expand $HOME like `state` does."""
    monkeypatch.delenv('REMOTE_SSH_RUNTIME_BASE', raising=False)
    monkeypatch.delenv('REMOTE_SSH_MSG_BASE', raising=False)
    commands = (ROOT / 'hooks' / 'hooks.json').read_text()
    assert str(state.runtime_base()).startswith('/tmp/remote-ssh-')
    assert '${REMOTE_SSH_RUNTIME_BASE:-/tmp/remote-ssh-*}' in commands
    assert state.msg_base() == Path.home() / '.claude' / 'remote-ssh'
    assert '${REMOTE_SSH_MSG_BASE:-$HOME/.claude/remote-ssh}' in commands


#---------------------------------------------------------------------
# main
#---------------------------------------------------------------------

def test_main_prints_json_and_tolerates_bad_input(runtime: state.Runtime, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    set_redirect(runtime)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(BASH_INPUT)))
    hooks.main('PreToolUse')
    assert json.loads(capsys.readouterr().out)['hookSpecificOutput']['permissionDecision'] == 'ask'

    monkeypatch.setattr('sys.stdin', io.StringIO('{not json'))
    hooks.main('PreToolUse')
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'remote-ssh hook' in captured.err
