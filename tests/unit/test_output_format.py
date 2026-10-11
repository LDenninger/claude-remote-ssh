"""Unit tests that pin the output grammar of every tool result: first line, field order, blocks, errors."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from remote_ssh import relay, server, state
from remote_ssh.conn import ConnectError

FIRST_LINE = re.compile(r'^(connect|disconnect|exec|mode|sessions|send|inbox|status)( [\w-]+)?( · [^\n·]+)*$')


def keys(text: str) -> list[str]:
    """Return the keys of the leading `key: value` lines of a result, in order."""
    found = []
    for line in text.split('\n')[1:]:
        match = re.match(r'^([a-z_]+): ', line)
        if not match:
            break
        found.append(match.group(1))
    return found


def blocks(text: str) -> list[str]:
    """Return the titles of the `--- <name> ---` blocks of a result, in order."""
    return re.findall(r'^--- (.+) ---$', text, re.MULTILINE)


def assert_error(text: str, tool: str, host: str | None, reason: str) -> None:
    """Assert the shape of an error: first line, `reason:` right after it, `next:` as the last field."""
    lines = text.split('\n')
    assert lines[0] == (f'{tool} {host} · error' if host else f'{tool} · error')
    assert lines[1] == f'reason: {reason}'
    assert keys(text)[-1] == 'next' and keys(text)[0] == 'reason'


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> server.Server:
    """Return a server with a runtime owned by the test process, session sid-0001 and a local store."""
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt'))
    monkeypatch.setenv('REMOTE_SSH_CLAUDE_PID', str(os.getpid()))
    monkeypatch.setenv('REMOTE_SSH_MSG_BASE', str(tmp_path / 'msg'))
    monkeypatch.setattr(server.relay, 'remote_register', lambda master, sid, entry: None)
    monkeypatch.setattr(server.relay, 'remote_unregister', lambda master, sid, timeout=30: None)
    monkeypatch.setattr(server.relay, 'remote_cleanup', lambda master, hostname, live_sids: None)
    runtime = state.Runtime.current()
    runtime.update_owner(session_id='sid-0001', cwd='/w/proj', name='proj-sid-')
    return server.Server(runtime, server.Config(), store=relay.Store(tmp_path / 'msg'), on_exit=lambda: None)


def stub_host(app: server.Server, alias: str = 'gpu', **fields: Any) -> SimpleNamespace:
    """Register a connected stub host session on `app`."""
    host = SimpleNamespace(**{'alias': alias, 'host': 'h', 'status': 'connected', 'error': None, 'abs_path': '/p', 'lock': threading.Lock(),
                              'master': SimpleNamespace(), 'mounted_fs': True, 'mounted_mem': True, 'fs_dir': Path('/rt/gpu/fs'),
                              'mem_dir': Path('/rt/gpu/mem'), 'is_healthy': lambda: True, 'memory_block': lambda: '', **fields})
    app.hosts[alias] = host
    return host


#---------------------------------------------------------------------
# connect, disconnect
#---------------------------------------------------------------------

def test_connect_shape(app: server.Server, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        def __init__(self, alias: str, host: str, path: str, *rest: Any) -> None:
            self.alias, self.host, self.status, self.error = alias, host, 'connecting', None
            self.lock, self.master = threading.Lock(), SimpleNamespace()
            self.abs_path, self.fs_dir, self.mem_dir = '/home/u/train', Path('/rt/gpu/fs'), Path('/rt/gpu/mem')
            self.mounted_fs, self.mounted_mem = True, False

        def connect(self) -> list[str]:
            self.status = 'connected'
            return ['no remote memory directory at /m, memory mount skipped']

        def memory_block(self) -> str:
            return ''

        def to_state(self) -> dict:
            return {'host': self.host, 'status': self.status}

    monkeypatch.setattr(server, 'HostSession', FakeSession)
    monkeypatch.setattr(server.relay, 'remote_sessions', lambda master, timeout=30: [{'name': 'train-befb', 'session_id': 'befb02c8'}, {'name': 'me', 'session_id': 'sid-0001'}])
    text, is_error = app.call_tool('remote_connect', {'alias': 'gpu', 'host': 'h', 'path': '~/train'})
    assert not is_error
    assert text == ('connect gpu · 1 session · bash local\n'
                    'path: /home/u/train\nfiles: /rt/gpu/fs\nmemory: not mounted\nsessions: train-befb\nredirect: remote_mode("gpu")\n'
                    'warning: no remote memory directory at /m, memory mount skipped')


def test_connect_failure_shape(app: server.Server, monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing:
        def __init__(self, alias: str, host: str, *rest: Any) -> None:
            self.alias, self.host, self.lock, self.status, self.error = alias, host, threading.Lock(), 'connecting', None

        def connect(self) -> list[str]:
            raise ConnectError('ssh to h failed\nPermission denied (publickey).')

        def to_state(self) -> dict:
            return {'host': self.host, 'status': self.status}

    monkeypatch.setattr(server, 'HostSession', Failing)
    text, is_error = app.call_tool('remote_connect', {'alias': 'gpu', 'host': 'h', 'path': '~'})
    assert is_error
    assert_error(text, 'connect', 'gpu', 'ssh to h failed')
    assert blocks(text) == ['detail from gpu (data)'] and text.endswith('> Permission denied (publickey).')


def test_connect_rejects_bad_input_and_repeat(app: server.Server) -> None:
    text, is_error = app.call_tool('remote_connect', {'alias': 'gpu', 'host': '-oProxy', 'path': '~'})
    assert is_error
    assert_error(text, 'connect', 'gpu', "invalid host '-oProxy': it must be non-empty and must not start with -")
    stub_host(app)
    text, _ = app.call_tool('remote_connect', {'alias': 'gpu', 'host': 'h', 'path': '~'})
    assert_error(text, 'connect', 'gpu', 'already connected (h)')
    text, _ = app.call_tool('remote_connect', {'alias': 'gpu'})
    assert_error(text, 'connect', 'gpu', 'missing argument(s) host, path')


def test_disconnect_shape(app: server.Server) -> None:
    host = stub_host(app)
    host.disconnect = lambda store, sid: [('unmounted', 'files, memory'), ('pulled', '0')]
    text, is_error = app.call_tool('remote_disconnect', {'alias': 'gpu'})
    assert (text, is_error) == ('disconnect gpu\nunmounted: files, memory\npulled: 0\nnote: earlier memory links to gpu are stale', False)


def test_disconnect_unknown_is_error(app: server.Server) -> None:
    stub_host(app)
    text, is_error = app.call_tool('remote_disconnect', {'alias': 'gpu2'})
    assert is_error
    assert text == ('disconnect gpu2 · error\nreason: not connected\nconnected: gpu\n'
                    'next: ask the user for the ssh host of gpu2, then call remote_connect')


#---------------------------------------------------------------------
# exec, mode
#---------------------------------------------------------------------

@pytest.mark.parametrize('output, expected', [
    (('epoch-40.pt\n', '', 0), 'exec gpu · exit 0\n--- stdout ---\nepoch-40.pt\n'),
    (('', "ls: cannot access 'ckpt'\n", 2), "exec gpu · exit 2\n--- stderr ---\nls: cannot access 'ckpt'\n"),
    (('a\n', 'b\n', 1), 'exec gpu · exit 1\n--- stdout ---\na\n\n--- stderr ---\nb\n'),
    (('', '', 0), 'exec gpu · exit 0'),
])
def test_exec_shape(app: server.Server, output: tuple, expected: str) -> None:
    stub_host(app).master.exec = lambda command, cwd, timeout: output
    assert app.call_tool('remote_exec', {'alias': 'gpu', 'command': 'x'}) == (expected, False)


def test_exec_ssh_failure_is_error_with_streams(app: server.Server) -> None:
    stub_host(app).master.exec = lambda command, cwd, timeout: ('', 'Connection closed\n', 255)
    text, is_error = app.call_tool('remote_exec', {'alias': 'gpu', 'command': 'x'})
    assert is_error
    assert_error(text, 'exec', 'gpu', 'ssh failed (exit 255)')
    assert blocks(text) == ['stderr']


def test_exec_not_connected_and_bad_timeout(app: server.Server) -> None:
    text, is_error = app.call_tool('remote_exec', {'alias': 'gpu2', 'command': 'x'})
    assert is_error and keys(text) == ['reason', 'connected', 'next']
    assert_error(text, 'exec', 'gpu2', 'not connected')
    stub_host(app)
    text, _ = app.call_tool('remote_exec', {'alias': 'gpu', 'command': 'x', 'timeout_s': 0})
    assert_error(text, 'exec', 'gpu', 'timeout_s must be an integer from 1 to 3600, got 0')


def test_mode_shape(app: server.Server) -> None:
    stub_host(app)
    assert app.call_tool('remote_mode', {'alias': 'gpu'}) == ('mode gpu · bash redirected\nlocal: prefix a command with local: to run it here', False)
    assert app.call_tool('remote_mode', {'alias': None}) == ('mode · bash local', False)
    text, is_error = app.call_tool('remote_mode', {'alias': 'gpu2'})
    assert is_error
    assert_error(text, 'mode', 'gpu2', 'not connected')


def test_failed_host_error_carries_its_error_block(app: server.Server) -> None:
    stub_host(app, status='reconnecting', error='Connection refused')
    text, is_error = app.call_tool('remote_exec', {'alias': 'gpu', 'command': 'x'})
    assert is_error
    assert_error(text, 'exec', 'gpu', 'reconnecting')
    assert text.endswith('--- error from gpu (data) ---\n> Connection refused')


#---------------------------------------------------------------------
# sessions, send, inbox
#---------------------------------------------------------------------

def test_sessions_table(app: server.Server, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_host(app)
    monkeypatch.setattr(server.relay, 'remote_sessions', lambda master, timeout=30: [
        {'name': 'train-befb', 'session_id': 'befb02c8aaaa', 'cwd': '~/train'},
        {'name': 'me', 'session_id': 'sid-0001', 'cwd': '/w'},
        {'name': 'far', 'session_id': 'far00001', 'cwd': '/x', 'relay_via': 'laptop'}])
    text, is_error = app.call_tool('remote_sessions', {'alias': 'gpu'})
    assert not is_error
    assert text.split('\n') == ['sessions gpu · 3',
                                'name        id        cwd      status',
                                'train-befb  befb02c8  ~/train  -',
                                'me          sid-0001  /w       this session',
                                'far         far00001  /x       via laptop']


def test_sessions_local_and_empty(app: server.Server) -> None:
    assert app.call_tool('remote_sessions', {}) == ('sessions · 0', False)


def test_send_shape_and_errors(app: server.Server) -> None:
    app.store.register('peer-0001', {'name': 'train-befb'})
    text, is_error = app.call_tool('remote_send', {'session': 'train-befb', 'body': 'hi'})
    assert not is_error
    assert re.fullmatch(r'send · train-befb\nid: \d+-[0-9a-f]{8}\nroute: inbox', text)
    text, _ = app.call_tool('remote_send', {'session': 'nobody', 'body': 'hi'})
    assert_error(text, 'send', None, "no session 'nobody' on this machine")
    text, _ = app.call_tool('remote_send', {'session': 'train-befb', 'body': 'x' * 8001})
    assert_error(text, 'send', None, 'body is 8001 characters, the limit is 8000')
    assert text.endswith('next: shorten the body or send a file path')
    app.config.messaging = False
    text, _ = app.call_tool('remote_send', {'session': 'train-befb', 'body': 'hi'})
    assert_error(text, 'send', None, 'messaging is disabled in the plugin options')


def test_send_without_session_id_is_error(app: server.Server) -> None:
    app.runtime.update_owner(session_id=None)
    text, is_error = app.call_tool('remote_send', {'session': 'x', 'body': 'hi'})
    assert is_error
    assert_error(text, 'send', None, 'own session id unknown (SessionStart hook has not run)')


def test_inbox_shape(app: server.Server) -> None:
    assert app.call_tool('remote_inbox', {}) == ('inbox · 0 new · 0 delivered', False)
    msg = {**relay.new_message('peer-0001', 'train-befb', 'far', 'sid-0001', 'Finished at epoch 40.'), 'relayed_from': 'gpu', 'sent_at': '2026-10-11T02:56:01+00:00'}
    app.store.deliver(msg)
    text, is_error = app.call_tool('remote_inbox', {})
    assert not is_error
    assert text == ('inbox · 1 new · 1 delivered\n--- new ---\ngpu/train-befb  02:56:01Z\n> Finished at epoch 40.\n'
                    '--- delivered ---\ngpu/train-befb  02:56:01Z\n> Finished at epoch 40.')
    assert app.call_tool('remote_inbox', {})[0].startswith('inbox · 0 new · 1 delivered\n--- delivered ---\n')


#---------------------------------------------------------------------
# status
#---------------------------------------------------------------------

def test_status_shape(app: server.Server, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_host(app)
    monkeypatch.setattr(server.relay, 'remote_sessions', lambda master, timeout=30: [{'session_id': 'a'}, {'session_id': 'b'}])
    app.runtime.update_state(lambda saved: saved['hosts'].update(node2={'host': 'n2', 'status': 'reconnecting', 'error': 'timed out'}))
    app.runtime.update_state(lambda saved: saved.update(redirect='gpu'))
    monkeypatch.setattr(server.platform, 'missing', lambda tools: [])
    text, is_error = app.call_tool('remote_status', {})
    assert not is_error
    assert text.split('\n')[:4] == ['status · 2 hosts · redirect gpu',
                                    'host   state         files    memory   sessions',
                                    'gpu    connected     mounted  mounted  2',
                                    'node2  reconnecting  -        -        -']
    assert re.search(r'^messaging: on\nsession: sid-0001\nruntime: ', text, re.MULTILINE)
    assert blocks(text) == ['error from node2 (data)'] and text.endswith('> timed out')


def test_status_without_hosts(app: server.Server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.platform, 'missing', lambda tools: ['sshfs'])
    text, _ = app.call_tool('remote_status', {})
    assert text.split('\n')[0] == 'status · 0 hosts · redirect off'
    assert blocks(text) == ['missing tools']


#---------------------------------------------------------------------
# Invariants
#---------------------------------------------------------------------

def test_every_result_opens_with_the_first_line_grammar(app: server.Server, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_host(app).master.exec = lambda command, cwd, timeout: ('x', '', 0)
    monkeypatch.setattr(server.relay, 'remote_sessions', lambda master, timeout=30: [])
    calls = [('remote_exec', {'alias': 'gpu', 'command': 'x'}), ('remote_exec', {'alias': 'zz', 'command': 'x'}), ('remote_mode', {'alias': None}),
             ('remote_sessions', {}), ('remote_inbox', {}), ('remote_status', {}), ('remote_send', {'session': 'q', 'body': 'b'}), ('remote_disconnect', {'alias': 'zz'})]
    for name, args in calls:
        text, _ = app.call_tool(name, args)
        assert FIRST_LINE.match(text.split('\n')[0]), text
        assert not text.startswith('ok')


def test_no_runtime_is_an_error_in_the_grammar() -> None:
    app = server.Server(None, server.Config(), store=SimpleNamespace(), on_exit=lambda: None)
    text, is_error = app.call_tool('remote_exec', {'alias': 'gpu', 'command': 'x'})
    assert is_error
    assert_error(text, 'exec', 'gpu', server.NO_RUNTIME)
