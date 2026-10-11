"""Unit tests for remote_ssh.server: JSON-RPC framing, tool schemas and teardown-once."""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from remote_ssh import __version__, relay, server, state


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


def test_expand_remote_path() -> None:
    assert server.expand_remote_path('~') == '.'
    assert server.expand_remote_path('~/my proj') == "'my proj'"
    assert server.expand_remote_path('/srv/x') == '/srv/x'


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
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(server.relay, 'remote_register', lambda master, sid, entry: calls.append(('register', master.alias, sid)))
    monkeypatch.setattr(server.relay, 'remote_unregister', lambda master, sid: calls.append(('unregister', master.alias, sid)))
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
    assert calls == [('register', 'gpu-a', 'old-0001')]

    calls.clear()
    app.sync_session_id()
    assert calls == []

    app.runtime.update_owner(session_id='new-0002')
    app.sync_session_id()
    assert [entry['session_id'] for entry in app.store.sessions()] == ['new-0002']
    assert calls == [('unregister', 'gpu-a', 'old-0001'), ('register', 'gpu-a', 'new-0002')]
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
    reports = {'gpu-a': ('connected gpu-a (h:/p)\n\n## Remote memory (gpu-a)', False), 'gpu-b': ('remote_connect failed: gpu-b', True),
               'gpu-c': ('connected gpu-c (h:/p)', False)}
    monkeypatch.setattr(app, 'connect_host', lambda alias, host, path: reports[alias])
    app.runtime.update_owner(session_id='sid-0001', cwd='/w/proj')
    state.write_json_atomic(app.runtime.dir / 'autoconnect.json', [{'alias': 'gpu-a', 'host': 'h'}, {'alias': 'gpu-b', 'host': 'h'}])
    app.run_startup()
    assert wait_until(lambda: (app.runtime.context_dir / 'gpu-a.md').exists())
    text, is_error = app.call_tool('remote_connect', {'alias': 'gpu-c', 'host': 'h', 'path': '/p'})
    assert not is_error and text.startswith('connected gpu-c')
    time.sleep(0.2)
    [block] = app.runtime.claim_context()
    assert block.startswith('remote-ssh: background connect\nconnected gpu-a')
    assert '## Remote memory (gpu-a)' in block


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

    def pull_outbox(master: Any, sid: str, store: Any) -> int:
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
    assert pulls == ['sid-0001']
    assert (host.checks, host.status) == (0, 'connected')
    assert not host.lock.locked()


@pytest.mark.parametrize('listening, error', [(False, None), (True, server.relay.RemoteCommandError(255, 'mux failed'))])
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
    assert (host.status, host.reconnects, host.checks) == ('connected', 0, 0)


@pytest.mark.parametrize('healthy', [True, False])
def test_tick_without_messaging_uses_check_only(tick_app: tuple[server.Server, list], healthy: bool) -> None:
    app, pulls = tick_app
    app.config.messaging = False
    host = TickHost(healthy=healthy)
    app.hosts = {'gpu-a': host}
    app.tick()
    assert pulls == []
    assert host.checks == 1
    assert wait_until(lambda: host.reconnects == (0 if healthy else 1) and not host.lock.locked())


def test_teardown_removes_redirect_flag_and_keeps_target(live_app: tuple[server.Server, list]) -> None:
    app, _ = live_app
    app.runtime.update_state(lambda saved: saved.update(redirect='gpu-a'))
    assert app.runtime.redirect_flag_path.exists()
    app.teardown_all()
    assert not app.runtime.redirect_flag_path.exists()
    assert app.runtime.load_state()['redirect'] == 'gpu-a'


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
