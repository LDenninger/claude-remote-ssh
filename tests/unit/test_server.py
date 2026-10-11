"""Unit tests for remote_ssh.server: JSON-RPC framing, tool schemas and teardown-once."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from remote_ssh import __version__, memory, relay, server, state


class StubStore:
    """Message store that records unregister calls."""

    def __init__(self) -> None:
        self.unregistered: list[str] = []

    def unregister(self, sid: str) -> None:
        self.unregistered.append(sid)


class StubHost:
    """Host session that counts disconnects."""

    def __init__(self, alias: str) -> None:
        self.alias = alias
        self.status = 'connected'
        self.lock = threading.Lock()
        self.disconnects = 0

    def disconnect(self, store: Any, session_id: Any) -> list[str]:
        self.disconnects += 1
        return []


def run_rpc(lines: list[Any], app: server.Server) -> list[dict]:
    """Feed JSON-RPC lines through `serve` and return the decoded responses by order of arrival."""
    stdin = io.StringIO(''.join((line if isinstance(line, str) else json.dumps(line)) + '\n' for line in lines))
    stdout = io.StringIO()
    server.serve(stdin, stdout, app)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


@pytest.fixture
def app(tmp_path: Path) -> server.Server:
    """Return a server without a runtime, as when no Claude ancestor is found."""
    return server.Server(None, server.Config(), store=StubStore(), on_exit=lambda: None)


#---------------------------------------------------------------------
# JSON-RPC framing
#---------------------------------------------------------------------

def test_initialize_echoes_protocol_version(app: server.Server) -> None:
    [reply] = run_rpc([{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26', 'capabilities': {}}}], app)
    assert reply['id'] == 1
    assert reply['result']['protocolVersion'] == '2025-03-26'
    assert reply['result']['capabilities'] == {'tools': {}}
    assert reply['result']['serverInfo'] == {'name': 'remote-ssh', 'version': __version__}


def test_notification_gets_no_reply_and_ping_does(app: server.Server) -> None:
    replies = run_rpc([{'jsonrpc': '2.0', 'method': 'notifications/initialized'}, {'jsonrpc': '2.0', 'id': 'p', 'method': 'ping'}], app)
    assert replies == [{'jsonrpc': '2.0', 'id': 'p', 'result': {}}]


def test_tools_list_schemas(app: server.Server) -> None:
    [reply] = run_rpc([{'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}], app)
    tools = {tool['name']: tool for tool in reply['result']['tools']}
    assert set(tools) == {'remote_connect', 'remote_disconnect', 'remote_status', 'remote_exec',
                          'remote_mode', 'remote_sessions', 'remote_send', 'remote_inbox'}
    for tool in tools.values():
        schema = tool['inputSchema']
        assert tool['description']
        assert schema['type'] == 'object'
        assert set(schema.get('required', [])) <= set(schema['properties'])
    assert tools['remote_connect']['inputSchema']['required'] == ['alias', 'host', 'path']
    assert tools['remote_send']['inputSchema']['required'] == ['session', 'body']
    assert 'null' in tools['remote_mode']['inputSchema']['properties']['alias']['type']


def test_unknown_method_and_parse_error(app: server.Server) -> None:
    replies = run_rpc([{'jsonrpc': '2.0', 'id': 3, 'method': 'resources/list'}, '{broken'], app)
    assert replies[0]['id'] == 3
    assert replies[0]['error']['code'] == -32601
    assert replies[1]['error']['code'] == -32700


def test_tools_call_without_runtime_is_error(app: server.Server) -> None:
    [reply] = run_rpc([{'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'remote_status', 'arguments': {}}}], app)
    result = reply['result']
    assert result['isError'] is True
    assert server.NO_RUNTIME in result['content'][0]['text']
    assert result['content'][0]['type'] == 'text'


def test_tools_call_unknown_tool_is_invalid_params(app: server.Server) -> None:
    [reply] = run_rpc([{'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'nope', 'arguments': {}}}], app)
    assert reply['error']['code'] == -32602


#---------------------------------------------------------------------
# Config
#---------------------------------------------------------------------

def test_config_from_env_parses_substituted_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('REMOTE_SSH_MESSAGING', 'False')
    monkeypatch.setenv('REMOTE_SSH_POLL_INTERVAL', '2.0')
    monkeypatch.setenv('REMOTE_SSH_SSH_OPTS', '-p 2222 -o IdentitiesOnly=yes')
    config = server.Config.from_env()
    assert config.messaging is False
    assert config.poll_interval_s == 2.0
    assert config.ssh_opts == ['-p', '2222', '-o', 'IdentitiesOnly=yes']
    monkeypatch.setenv('REMOTE_SSH_MESSAGING', 'True')
    monkeypatch.setenv('REMOTE_SSH_POLL_INTERVAL', 'junk')
    config = server.Config.from_env()
    assert config.messaging is True
    assert config.poll_interval_s == 2.0


def run_resolve_script(path: str, home: Path) -> dict[str, str]:
    """Run `build_resolve_script` under sh with `home` as $HOME and parse its key=value lines."""
    result = subprocess.run(['sh', '-c', server.build_resolve_script(path)], env={**os.environ, 'HOME': str(home)},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return dict(line.split('=', 1) for line in result.stdout.splitlines())


def test_resolve_script_is_portable_and_resolves_paths(tmp_path: Path) -> None:
    home = tmp_path.resolve() / 'home'  # pwd -P resolves /var -> /private/var on macOS
    (home / 'my proj').mkdir(parents=True)
    (home / 'link').symlink_to(home / 'my proj')
    assert 'realpath' not in server.build_resolve_script('~/x')
    assert run_resolve_script('~', home) == {'home': str(home), 'path': str(home)}
    assert run_resolve_script('~/my proj', home)['path'] == str(home / 'my proj')
    assert run_resolve_script('link', home)['path'] == str(home / 'my proj')
    assert run_resolve_script(str(home / 'link'), home)['path'] == str(home / 'my proj')


@pytest.mark.skipif(shutil.which('git') is None, reason='git not installed')
def test_resolve_script_reports_main_worktree_root(tmp_path: Path) -> None:
    """Claude Code names a project's memory after the main worktree root, for subdirs and linked worktrees alike."""
    home = tmp_path.resolve()
    repo = home / 'repo'
    (repo / 'sub' / 'deeper').mkdir(parents=True)
    git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t']
    subprocess.run([*git, 'init', '-q', str(repo)], check=True)
    subprocess.run([*git, '-C', str(repo), 'commit', '-q', '--allow-empty', '-m', 'init'], check=True)
    subprocess.run([*git, '-C', str(repo), 'worktree', 'add', '-q', str(home / 'wt'), '-b', 'wt'], check=True)
    (home / 'wt' / 'sub2').mkdir()
    (home / 'plain').mkdir()
    assert run_resolve_script(str(repo), home)['root'] == str(repo)
    assert run_resolve_script('~/repo/sub/deeper', home) == {'home': str(home), 'path': str(repo / 'sub' / 'deeper'), 'root': str(repo)}
    assert run_resolve_script(str(home / 'wt' / 'sub2'), home)['root'] == str(repo)
    assert 'root' not in run_resolve_script(str(home / 'plain'), home)


def test_resolve_script_reports_missing_directory(tmp_path: Path) -> None:
    values = run_resolve_script('~/absent', tmp_path.resolve())
    assert values == {'home': str(tmp_path.resolve()), 'missing': '1'}


#---------------------------------------------------------------------
# Startup and session id changes
#---------------------------------------------------------------------

class StubMaster:
    """Master stand-in; registrations are recorded by the patched relay functions."""

    def __init__(self, alias: str) -> None:
        self.alias = alias


@pytest.fixture
def live_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[server.Server, list[tuple[str, str, str]]]:
    """Return a server with a runtime owned by the test process and a log of remote registry calls."""
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt'))
    monkeypatch.setenv('REMOTE_SSH_CLAUDE_PID', str(os.getpid()))
    monkeypatch.setenv('REMOTE_SSH_MSG_BASE', str(tmp_path / 'msg'))
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(server.relay, 'remote_register', lambda master, sid, entry: calls.append(('register', master.alias, sid, entry['relay_via'])))
    monkeypatch.setattr(server.relay, 'remote_unregister', lambda master, sid, timeout=30: calls.append(('unregister', master.alias, sid)))
    monkeypatch.setattr(server.relay, 'remote_cleanup', lambda master, hostname, live_sids: None)
    runtime = state.Runtime.current()
    app = server.Server(runtime, server.Config(ssh_opts=['-p', '2222']), store=relay.Store(tmp_path / 'msg'), on_exit=lambda: None)
    return app, calls


def test_sync_session_id_moves_registrations_after_clear(live_app: tuple[server.Server, list]) -> None:
    app, calls = live_app
    host = StubHost('gpu-a')
    host.master = StubMaster('gpu-a')
    app.hosts = {'gpu-a': host}
    app.runtime.update_owner(session_id='old-0001', cwd='/w/proj')
    app.sync_session_id()
    assert [entry['session_id'] for entry in app.store.sessions()] == ['old-0001']
    assert calls == [('register', 'gpu-a', 'old-0001', state.machine_id())]

    calls.clear()
    app.sync_session_id()
    assert calls == []

    app.runtime.update_owner(session_id='new-0002')
    app.sync_session_id()
    assert [entry['session_id'] for entry in app.store.sessions()] == ['new-0002']
    assert calls == [('unregister', 'gpu-a', 'old-0001'), ('register', 'gpu-a', 'new-0002', state.machine_id())]
    assert app.messaging_session_id() == 'new-0002'


def test_sync_session_id_with_messaging_off_registers_nothing(live_app: tuple[server.Server, list]) -> None:
    app, calls = live_app
    app.config.messaging = False
    app.runtime.update_owner(session_id='old-0001')
    app.sync_session_id()
    assert app.store.sessions() == []
    assert app.registered_sid is None


def test_startup_saves_ssh_opts_for_hooks(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    app.runtime.update_owner(session_id='sid-0001', cwd='/w/proj')
    app.run_startup()
    assert app.runtime.load_state()['ssh_opts'] == ['-p', '2222']
    assert [entry['session_id'] for entry in app.store.sessions()] == ['sid-0001']


def wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    """Poll `predicate` every 10 ms until it is truthy or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return bool(predicate())


def test_background_connect_queues_report_and_tool_connect_does_not(live_app: tuple[server.Server, list], monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = live_app
    reports = {'gpu-a': ('connect gpu-a · background\n--- memory index from gpu-a (data; links relative to memory:) ---\n> - note', False),
               'gpu-b': ('connect gpu-b · error', True), 'gpu-c': ('connect gpu-c · 0 sessions', False)}
    monkeypatch.setattr(app, 'connect_host', lambda alias, host, path, background=False: reports[alias])
    app.runtime.update_owner(session_id='sid-0001', cwd='/w/proj')
    state.write_json_atomic(app.runtime.dir / 'autoconnect.json', [{'alias': 'gpu-a', 'host': 'h'}, {'alias': 'gpu-b', 'host': 'h'}])
    app.run_startup()
    assert wait_until(lambda: (app.runtime.context_dir / 'gpu-a.md').exists())
    text, is_error = app.call_tool('remote_connect', {'alias': 'gpu-c', 'host': 'h', 'path': '/p'})
    assert not is_error and text.startswith('connect gpu-c')
    time.sleep(0.2)
    [block] = app.runtime.claim_context()
    assert block.startswith('connect gpu-a · background\n')
    assert '--- memory index from gpu-a' in block


class TickHost:
    """Host session stand-in for `tick`: a master probe, `-O check` and reconnect counters."""

    def __init__(self, listening: bool = True, healthy: bool = True) -> None:
        self.alias, self.host, self.status, self.error = 'gpu-a', 'h', 'connected', None
        self.lock = threading.Lock()
        self.retry_at, self.retry_delay = 0.0, 1.0
        self.master = SimpleNamespace(alias='gpu-a', is_listening=lambda: listening)
        self.healthy = healthy
        self.checks = self.reconnects = 0

    def is_healthy(self) -> bool:
        self.checks += 1
        return self.healthy

    def reconnect(self) -> None:
        self.reconnects += 1

    def to_state(self) -> dict[str, Any]:
        return {'host': self.host, 'status': self.status}


@pytest.fixture
def tick_app(live_app: tuple[server.Server, list], monkeypatch: pytest.MonkeyPatch) -> tuple[server.Server, list[Any]]:
    """Return a live server whose outbox pulls are recorded, and the list that records them.

    Appending an exception to the list makes the next pull raise it instead.
    """
    app, _ = live_app
    app.runtime.update_owner(session_id='sid-0001', cwd='/w/proj')
    pulls: list[Any] = []

    def pull_outbox(master: Any, sid: str, store: Any, timeout: float = 30) -> int:
        assert timeout <= server.POLL_PULL_TIMEOUT_S
        if pulls and isinstance(pulls[-1], Exception):
            raise pulls.pop()
        pulls.append(sid)
        return 0

    monkeypatch.setattr(server.relay, 'pull_outbox', pull_outbox)
    return app, pulls


def test_tick_with_messaging_pulls_once_without_check(tick_app: tuple[server.Server, list]) -> None:
    app, pulls = tick_app
    host = TickHost()
    app.hosts = {'gpu-a': host}
    app.tick()
    assert wait_until(lambda: pulls == ['sid-0001'] and not host.lock.locked())
    assert (host.checks, host.status) == (0, 'connected')


@pytest.mark.parametrize('listening, error', [(False, None), (True, server.relay.RemoteCommandError(255, 'mux failed')),
                                              (True, subprocess.TimeoutExpired('ssh', 5))])
def test_tick_with_messaging_detects_dead_master(tick_app: tuple[server.Server, list], listening: bool, error: Exception | None) -> None:
    app, pulls = tick_app
    if error is not None:
        pulls.append(error)
    host = TickHost(listening=listening)
    app.hosts = {'gpu-a': host}
    app.tick()
    assert wait_until(lambda: host.reconnects == 1 and not host.lock.locked())
    assert host.checks == 0
    assert pulls == []
    assert app.runtime.load_state()['hosts']['gpu-a']['status'] == 'reconnecting'


def test_tick_keeps_host_on_remote_command_failure(tick_app: tuple[server.Server, list]) -> None:
    app, pulls = tick_app
    pulls.append(server.relay.RemoteCommandError(1, 'disk full'))
    host = TickHost()
    app.hosts = {'gpu-a': host}
    app.tick()
    assert wait_until(lambda: not pulls and not host.lock.locked())
    assert (host.status, host.reconnects, host.checks) == ('connected', 0, 0)


@pytest.mark.parametrize('healthy', [True, False])
def test_tick_without_messaging_uses_check_only(tick_app: tuple[server.Server, list], healthy: bool) -> None:
    app, pulls = tick_app
    app.config.messaging = False
    host = TickHost(healthy=healthy)
    app.hosts = {'gpu-a': host}
    app.tick()
    assert wait_until(lambda: host.checks == 1)
    assert pulls == []
    assert wait_until(lambda: host.reconnects == (0 if healthy else 1) and not host.lock.locked())


def test_teardown_keeps_redirect_target_and_flag(live_app: tuple[server.Server, list]) -> None:
    """A redirected Bash call must reach the hook and be denied until a restarted server reconnects."""
    app, _ = live_app
    app.runtime.update_state(lambda saved: saved.update(redirect='gpu-a'))
    app.runtime.pending_path.touch()
    app.teardown_all()
    assert app.runtime.redirect_flag_path.exists()
    assert app.runtime.load_state()['redirect'] == 'gpu-a'
    assert not app.runtime.pending_path.exists()


def test_session_end_disconnects_without_exiting(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    exits = []
    app.on_exit = lambda: exits.append(True)
    host = StubHost('gpu-a')
    host.to_state = lambda: {'host': 'h', 'status': host.status}
    app.hosts = {'gpu-a': host}
    app.runtime.update_owner(session_id='sid-0001', cwd='/w/proj')
    app.runtime.update_state(lambda saved: saved.update(hosts={'gpu-a': {'host': 'h', 'status': 'connected'}}, redirect='gpu-a'))
    app.runtime.queue_context('gpu-a', 'stale report')
    app.runtime.shutdown_path.touch()
    app.tick()
    assert (host.disconnects, exits, app.hosts) == (1, [], {})
    assert not app.runtime.shutdown_path.exists()
    saved = app.runtime.load_state()
    assert (saved['hosts'], saved['redirect']) == ({}, None)
    assert not app.runtime.redirect_flag_path.exists()
    assert app.runtime.claim_context() == []
    #--- the loop keeps running, and a later session id registers anew ---
    assert not app._stop.is_set()
    app.tick()
    assert [entry['session_id'] for entry in app.store.sessions()] == ['sid-0001']


def test_tick_maintains_pending_flag(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    app.runtime.update_owner(session_id='sid-0001', cwd='/w/proj')
    app.tick()
    assert not app.runtime.pending_path.exists()
    msg = relay.new_message('other-0001', 'other', 'here', 'sid-0001', 'hi')
    app.store.deliver(msg)
    app.tick()
    assert app.runtime.pending_path.exists()
    app.store.claim('sid-0001', msg['id'])
    app.tick()
    assert not app.runtime.pending_path.exists()


def test_reconnect_releases_lock_when_saving_fails(live_app: tuple[server.Server, list], monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = live_app
    host = TickHost()
    app.hosts = {'gpu-a': host}
    host.lock.acquire()
    monkeypatch.setattr(app.runtime, 'update_state', lambda fn: 1 / 0)
    try:
        app._Server__reconnect(host)
    except ZeroDivisionError:
        pass
    assert not host.lock.locked()


def test_reconnect_of_dropped_host_releases_what_it_started(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    host = TickHost()
    releases = []
    host.release = lambda: releases.append(True) or []
    host.lock.acquire()
    app._Server__reconnect(host)
    assert releases == [True]
    assert not host.lock.locked()


def test_tick_does_not_wait_for_a_slow_host(tick_app: tuple[server.Server, list], monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = tick_app
    release = threading.Event()
    monkeypatch.setattr(server.relay, 'pull_outbox', lambda master, sid, store, timeout=30: release.wait(5) and 0)
    host = TickHost()
    app.hosts = {'gpu-a': host}
    started = time.monotonic()
    app.tick()
    assert time.monotonic() - started < 1
    assert host.lock.locked()
    release.set()
    assert wait_until(lambda: not host.lock.locked())


def test_teardown_disconnects_hosts_in_parallel(app: server.Server) -> None:
    hosts = [StubHost('gpu-a'), StubHost('gpu-b'), StubHost('gpu-c')]
    for host in hosts:
        host.disconnect = lambda store, sid, host=host: time.sleep(1) or host.__dict__.update(disconnects=1) or []
    app.hosts = {host.alias: host for host in hosts}
    started = time.monotonic()
    app.teardown_all()
    assert time.monotonic() - started < 2
    assert [host.disconnects for host in hosts] == [1, 1, 1]


def test_startup_marks_autoconnect_read(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    app.runtime.update_owner(session_id='sid-0001', cwd='/w/proj')
    app.run_startup()
    assert app.runtime.owner()['autoconnect_read'] is True


#---------------------------------------------------------------------
# Tools
#---------------------------------------------------------------------

@pytest.mark.parametrize('host', ['-oProxyCommand=touch /tmp/x', ''])
def test_connect_rejects_host_that_ssh_would_parse_as_option(live_app: tuple[server.Server, list], host: str) -> None:
    app, _ = live_app
    text, is_error = app.call_tool('remote_connect', {'alias': 'gpu-a', 'host': host, 'path': '~'})
    assert is_error and 'invalid host' in text
    assert app.hosts == {}


def test_unknown_alias_error_names_connected_aliases(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    app.hosts['gpu-a'] = StubHost('gpu-a')
    text, is_error = app.call_tool('remote_exec', {'alias': 'gpu-b', 'command': 'true'})
    assert is_error
    assert text == ('exec gpu-b · error\nreason: not connected\nconnected: gpu-a\n'
                    'next: ask the user for the ssh host of gpu-b, then call remote_connect')


def test_exec_result_omits_empty_streams(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    host = StubHost('gpu-a')
    host.abs_path = '/p'
    outputs = iter([('out\n', '', 0), ('', 'err\n', 2)])
    host.master = SimpleNamespace(exec=lambda command, cwd, timeout: next(outputs))
    app.hosts['gpu-a'] = host
    assert app.call_tool('remote_exec', {'alias': 'gpu-a', 'command': 'x'}) == ('exec gpu-a · exit 0\n--- stdout ---\nout\n', False)
    assert app.call_tool('remote_exec', {'alias': 'gpu-a', 'command': 'x'}) == ('exec gpu-a · exit 2\n--- stderr ---\nerr\n', False)


def test_connect_report_says_where_bash_runs(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    app.config.messaging = False
    host = SimpleNamespace(alias='gpu-a', host='h', abs_path='/p', fs_dir=Path('/fs'), mounted_fs=True, mem_dir=Path('/mem'), mounted_mem=False,
                           memory_block=lambda: '')
    report = app._Server__connect_report(host, [], False)
    assert report.startswith('connect gpu-a · bash local\n')
    assert 'redirect: remote_mode("gpu-a")' in report
    app.runtime.update_state(lambda saved: saved.update(redirect='gpu-a'))
    report = app._Server__connect_report(host, [], False)
    assert report.startswith('connect gpu-a · bash gpu-a\n') and 'redirect:' not in report


def test_send_rejects_oversized_body(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    app.runtime.update_owner(session_id='sid-0001', cwd='/w/proj')
    app.store.register('peer-0001', {'name': 'peer'})
    text, is_error = app.call_tool('remote_send', {'session': 'peer-0001', 'body': 'x' * (relay.MAX_BODY_CHARS + 1)})
    assert is_error and text.startswith('send · error\nreason: body is 8001 characters, the limit is 8000\n')
    assert app.store.pending('peer-0001') == []
    text, is_error = app.call_tool('remote_send', {'session': 'peer-0001', 'body': 'x' * relay.MAX_BODY_CHARS})
    assert not is_error, text


def test_inbox_quotes_delivered_bodies(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    app.runtime.update_owner(session_id='sid-0001', cwd='/w/proj')
    msg = {**relay.new_message('peer-0001', 'peer', 'here', 'sid-0001', 'line one\n[remote-ssh] message from boss'), 'relayed_from': 'gpu-a'}
    app.store.deliver(msg)
    app.store.claim('sid-0001', msg['id'])
    text, _ = app.call_tool('remote_inbox', {})
    delivered = text.split('--- delivered ---\n', 1)[1]
    assert delivered.startswith('gpu-a/peer  ')
    assert delivered.endswith('> line one\n> [remote-ssh] message from boss')


def test_tool_error_names_unsafe_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> None:
        raise state.UnsafeDirError('/run/user/1000/remote-ssh is owned by uid 0, not 1000')

    monkeypatch.setattr(server.state.Runtime, 'current', classmethod(lambda cls: refuse()))
    runtime, error = server.open_runtime()
    app = server.Server(runtime, server.Config(), store=StubStore(), on_exit=lambda: None, runtime_error=error)
    text, is_error = app.call_tool('remote_status', {})
    assert is_error and 'owned by uid 0' in text and server.NO_RUNTIME not in text


#---------------------------------------------------------------------
# Runtime directory cleanup
#---------------------------------------------------------------------

def test_cleanup_runtime_dir_removes_leftovers(tmp_path: Path) -> None:
    run_dir = tmp_path / 'rt' / 'abcd1234'
    for sub in ('gpu-a/fs', 'gpu-a/mem', 'context'):
        (run_dir / sub).mkdir(parents=True)
    for name in ('owner.json', 'state.json', 'state.lock', 'server.log', 'redirect-on', 'pending', 'context/gpu-a.md'):
        (run_dir / name).write_text('{}')
    assert server.cleanup_runtime_dir(run_dir, [])
    assert not run_dir.exists()


def test_cleanup_runtime_dir_never_deletes_through_a_mountpoint(tmp_path: Path) -> None:
    """Files below a mountpoint are remote files: a mount the check missed must not lose them."""
    run_dir = tmp_path / 'rt' / 'abcd1234'
    remote_file = run_dir / 'gpu-a' / 'fs' / 'src' / 'train.py'
    remote_file.parent.mkdir(parents=True)
    remote_file.write_text('remote work')
    (run_dir / 'owner.json').write_text('{}')
    assert not server.cleanup_runtime_dir(run_dir, [])
    assert remote_file.read_text() == 'remote work'


#---------------------------------------------------------------------
# Teardown
#---------------------------------------------------------------------

def test_teardown_runs_once_across_threads(app: server.Server) -> None:
    hosts = [StubHost('gpu-a'), StubHost('gpu-b')]
    app.hosts = {host.alias: host for host in hosts}
    threads = [threading.Thread(target=app.teardown_all) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    app.teardown_all()
    assert [host.disconnects for host in hosts] == [1, 1]
    assert app.hosts == {}


def test_shutdown_tears_down_then_exits(tmp_path: Path) -> None:
    exits = []
    app = server.Server(None, server.Config(), store=StubStore(), on_exit=lambda: exits.append(True))
    host = StubHost('gpu-a')
    app.hosts = {'gpu-a': host}
    app.shutdown()
    app.shutdown()
    assert host.disconnects == 1
    assert exits == [True, True]


def test_config_treats_unresolved_user_config_placeholders_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    #--- an option the user never set may arrive as its literal ${user_config.*} placeholder ---
    monkeypatch.setenv('REMOTE_SSH_MESSAGING', '${user_config.messaging}')
    monkeypatch.setenv('REMOTE_SSH_POLL_INTERVAL', '${user_config.poll_interval_s}')
    monkeypatch.setenv('REMOTE_SSH_SSHFS_OPTIONS', '${user_config.sshfs_options}')
    config = server.Config.from_env()
    assert config.messaging is True
    assert config.poll_interval_s == 2.0
    assert config.sshfs_options == ''


#---------------------------------------------------------------------
# Messaging tools
#---------------------------------------------------------------------

def _stamped_msg(body: str = 'hi', to: str = 'sid', **fields: str) -> dict:
    return {**relay.new_message('abcdef1234567890', 'proj-abcd', 'laptop', to, body), 'sent_at': '2026-10-11T12:00:00+00:00', **fields}


@pytest.fixture
def remote_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / 'remote'
    monkeypatch.setenv('REMOTE_SSH_REMOTE_BASE', str(base))
    return base


class ShellMaster:
    """Run remote commands locally through ``sh``."""

    alias = 'gpu-a'

    def run(self, command: str, cwd: str | None = None, input: bytes | None = None, timeout: float | None = 60) -> subprocess.CompletedProcess:
        return subprocess.run(['sh', '-c', command], cwd=cwd, input=input, capture_output=True, timeout=timeout)


@pytest.fixture
def messaging_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> server.Server:
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt'))
    monkeypatch.setenv('REMOTE_SSH_CLAUDE_PID', str(os.getpid()))
    monkeypatch.setenv('REMOTE_SSH_MSG_BASE', str(tmp_path / 'msg'))
    runtime = state.Runtime.current()
    runtime.update_owner(session_id='sid-0001', cwd='/w/proj', name='proj-sid-')
    app = server.Server(runtime, server.Config(), store=relay.Store(tmp_path / 'msg'), on_exit=lambda: None)
    app._Server__connected = lambda tool, alias: (SimpleNamespace(master=ShellMaster()), '')
    return app


def test_remote_sessions_output_is_sanitized(messaging_app: server.Server, remote_base: Path) -> None:
    relay.Store(remote_base).register('peer-0001', {'name': 'bad\nname' + 'x' * 100, 'cwd': '/a\nb', 'host': 'h\nost', 'relay_via': 'm\nid'})
    text, is_error = messaging_app.call_tool('remote_sessions', {'alias': 'h'})
    assert not is_error
    assert len(text.split('\n')) == 3
    assert 'x' * 65 not in text


def test_send_limit_is_8000_characters(messaging_app: server.Server) -> None:
    messaging_app.store.register('peer-0001', {'name': 'peer'})
    assert relay.MAX_BODY_CHARS == 8000
    _, is_error = messaging_app.call_tool('remote_send', {'session': 'peer-0001', 'body': 'é' * 8001})
    assert is_error
    _, is_error = messaging_app.call_tool('remote_send', {'session': 'peer-0001', 'body': 'é' * 8000})
    assert not is_error


def test_remote_inbox_tool_claims_and_shows_everything(messaging_app: server.Server) -> None:
    for ii in range(5):
        messaging_app.store.deliver({**_stamped_msg('x' * 3000, to='sid-0001'), 'id': f'{1000 + ii}-0000000{ii}'})
    text, _ = messaging_app.call_tool('remote_inbox', {})
    assert text.startswith('inbox · 5 new · 0 delivered\n--- new ---\n') and messaging_app.store.pending('sid-0001') == []
    assert text.count('x' * 3000) == 5


def test_remote_send_to_relayed_target_goes_to_remote_outbox(messaging_app: server.Server, remote_base: Path) -> None:
    remote_store = relay.Store(remote_base)
    remote_store.register('relayed-0001', {'name': 'there', 'relay_via': 'machine-b'})
    remote_store.register('onhost-0001', {'name': 'here'})
    text, is_error = messaging_app.call_tool('remote_send', {'alias': 'h', 'session': 'relayed-0001', 'body': 'hello'})
    assert not is_error and 'route: outbox' in text, text
    assert len(list((remote_base / 'outbox' / 'relayed-0001').glob('*.json'))) == 1
    assert not (remote_base / 'inbox' / 'relayed-0001').exists()
    text, is_error = messaging_app.call_tool('remote_send', {'alias': 'h', 'session': 'onhost-0001', 'body': 'hello'})
    assert not is_error and 'route: inbox' in text, text
    assert len(list((remote_base / 'inbox' / 'onhost-0001').glob('*.json'))) == 1


def test_server_writes_messaging_flag_to_state(messaging_app: server.Server) -> None:
    messaging_app.config.messaging = False
    messaging_app.run_startup()
    assert messaging_app.runtime.load_state()['messaging'] is False


#---------------------------------------------------------------------
# remote_exec validation and error flag
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
    assert app_with_host.call_tool('remote_exec', args) == ('exec gpu-a · exit 0', False)


@pytest.mark.parametrize('code, is_error', [(0, False), (1, False), (2, False), (123, False), (124, True), (137, True), (255, True)])
def test_remote_exec_error_flag_only_for_timeout_signal_or_ssh_failure(app_with_host: server.Server, code: int, is_error: bool) -> None:
    app_with_host.stub.outputs = [('found\n', '', code)]
    text, flag = app_with_host.call_tool('remote_exec', {'alias': 'gpu-a', 'command': 'grep x f'})
    assert flag is is_error
    assert text.startswith('exec gpu-a · error' if code == 255 else f'exec gpu-a · exit {code}') and 'found' in text


#---------------------------------------------------------------------
# Remote memory index
#---------------------------------------------------------------------

@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> state.Runtime:
    """Return an isolated runtime owned by the test process."""
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt'))
    monkeypatch.setenv('REMOTE_SSH_MSG_BASE', str(tmp_path / 'msg'))
    monkeypatch.setenv('REMOTE_SSH_CLAUDE_PID', str(os.getpid()))
    return state.Runtime.current()


class MemMaster:
    """Master stand-in whose `run` records its arguments and returns or raises a canned result."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, Any]] = []

    def run(self, command: str, timeout: Any = 60, **kwargs: Any) -> Any:
        self.calls.append((command, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def host_session(runtime: state.Runtime, master: MemMaster) -> server.HostSession:
    host = server.HostSession('gpu-a', 'h', '/p', runtime, server.Config(), {})
    host.master, host.mounted_mem, host.mem_uri = master, True, '/home/u/.claude/projects/-p/memory'
    return host


def test_memory_block_reads_over_ssh_with_a_timeout(runtime: state.Runtime) -> None:
    host = host_session(runtime, MemMaster(SimpleNamespace(returncode=0, stdout=b'# Remote index\n- [A](a.md)\n')))
    (host.mem_dir).mkdir(parents=True, exist_ok=True)
    (host.mem_dir / 'MEMORY.md').write_text('FROM THE MOUNT')
    block = host.memory_block()
    assert '[A](a.md)' in block and 'FROM THE MOUNT' not in block
    [(command, timeout)] = host.master.calls
    assert 'MEMORY.md' in command and timeout == server.MEMORY_READ_TIMEOUT_S <= 3
    assert memory.cache_path(runtime.dir, 'gpu-a').read_text() == '# Remote index\n- [A](a.md)\n'


def test_memory_block_survives_a_hung_host(runtime: state.Runtime) -> None:
    host = host_session(runtime, MemMaster(subprocess.TimeoutExpired('ssh', 3)))
    assert host.memory_block() == ''


#---------------------------------------------------------------------
# Autoconnect on a new session id and messages across /clear
#---------------------------------------------------------------------

@pytest.fixture
def session_app(runtime: state.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> server.Server:
    monkeypatch.setattr(server.relay, 'remote_register', lambda master, sid, entry: None)
    monkeypatch.setattr(server.relay, 'remote_unregister', lambda master, sid, timeout=30: None)
    monkeypatch.setattr(server.relay, 'remote_cleanup', lambda master, machine, live: None)
    return server.Server(runtime, server.Config(), store=relay.Store(tmp_path / 'msg'), on_exit=lambda: None)


def test_new_session_id_connects_hosts_that_are_not_connected(session_app: server.Server, monkeypatch: pytest.MonkeyPatch) -> None:
    connected: list[str] = []
    monkeypatch.setattr(session_app, 'connect_host', lambda alias, host, path, background=False: connected.append(alias) or ('connected', False))
    session_app.runtime.update_owner(session_id='sid-0001', cwd='/w')
    state.write_json_atomic(session_app.runtime.dir / 'autoconnect.json', [{'alias': 'gpu-a', 'host': 'h'}])
    session_app.run_startup()
    assert wait_until(lambda: connected == ['gpu-a'])

    #--- unchanged id: nothing happens ---
    session_app.autoconnect_new_session()
    time.sleep(0.1)
    assert connected == ['gpu-a']

    #--- /resume into a session of another project: gpu-a is held, gpu-b is new ---
    session_app.hosts['gpu-a'] = SimpleNamespace(status='connected')
    state.write_json_atomic(session_app.runtime.dir / 'autoconnect.json', [{'alias': 'gpu-a', 'host': 'h'}, {'alias': 'gpu-b', 'host': 'h2', 'path': '/p'}])
    session_app.runtime.update_owner(session_id='sid-0002')
    session_app.autoconnect_new_session()
    assert wait_until(lambda: connected == ['gpu-a', 'gpu-b'])
    session_app.autoconnect_new_session()
    time.sleep(0.1)
    assert connected == ['gpu-a', 'gpu-b']


def test_clear_moves_unread_messages_and_pulls_the_old_outbox(session_app: server.Server, monkeypatch: pytest.MonkeyPatch) -> None:
    pulls: list[str] = []

    def pull(master: Any, sid: str, store: relay.Store, timeout: float = 30) -> int:
        pulls.append(sid)
        store.deliver(relay.new_message('remote-0001', 'peer', 'gpu-a', sid, 'late from the host'))
        return 1

    monkeypatch.setattr(server.relay, 'pull_outbox', pull)
    session_app.hosts = {'gpu-a': SimpleNamespace(alias='gpu-a', status='connected', master=SimpleNamespace(alias='gpu-a'))}
    session_app.runtime.update_owner(session_id='old-0001', cwd='/w')
    session_app.sync_session_id()
    session_app.store.deliver(relay.new_message('peer-0001', 'peer', 'here', 'old-0001', 'sent before clear'))

    session_app.runtime.update_owner(session_id='new-0002')
    session_app.sync_session_id()
    assert pulls == ['old-0001']
    assert not session_app.store.has_pending('old-0001')
    bodies = sorted(msg['body'] for msg in session_app.store.pending('new-0002'))
    assert bodies == ['late from the host', 'sent before clear']
    assert all(msg['to_session'] == 'new-0002' for msg in session_app.store.pending('new-0002'))


def test_initialize_returns_the_reply_instructions() -> None:
    reply = server.handle_message(None, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
    instructions = reply['result']['instructions']
    assert 'never message ids, routes, tool names' in instructions
    assert 'only when the user must do something' in instructions
