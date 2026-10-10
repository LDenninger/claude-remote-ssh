"""Unit tests for remote_ssh.hooks: redirect rewriting, SessionStart and message injection."""

from __future__ import annotations

import io
import json
import os
import shlex
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
