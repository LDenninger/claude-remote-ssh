"""Unit tests for remote_ssh.server: JSON-RPC framing, tool schemas and teardown-once."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from remote_ssh import __version__, server


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
