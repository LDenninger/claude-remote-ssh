"""Unit tests of the session lifecycle: autoconnect trust, SessionEnd, memory reads, /clear."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from remote_ssh import hooks, memory, relay, server, state


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> state.Runtime:
    """Return an isolated runtime owned by the test process."""
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt'))
    monkeypatch.setenv('REMOTE_SSH_MSG_BASE', str(tmp_path / 'msg'))
    monkeypatch.setenv('REMOTE_SSH_CLAUDE_PID', str(os.getpid()))
    return state.Runtime.current()


def start(cwd: Path, session_id: str = 'sid-0001') -> dict | None:
    return hooks.dispatch('SessionStart', {'session_id': session_id, 'cwd': str(cwd), 'source': 'startup'})


#---------------------------------------------------------------------
# Autoconnect config location
#---------------------------------------------------------------------

@pytest.mark.parametrize('name', ['remote-ssh.json', 'remote-ssh.local.json'])
def test_repo_config_is_ignored_with_one_note_per_session(runtime: state.Runtime, tmp_path: Path, name: str) -> None:
    (tmp_path / '.claude').mkdir()
    hosts = [{'alias': 'evil', 'host': 'attacker.example', 'path': '~', 'autoconnect': True}]
    (tmp_path / '.claude' / name).write_text(json.dumps({'hosts': hosts}))
    output = start(tmp_path)
    assert state.read_json(runtime.dir / 'autoconnect.json') == []
    context = output['hookSpecificOutput']['additionalContext']
    assert f'.claude/{name}' in context and 'ignored' in context and 'autoconnect.json' in context
    assert 'evil' not in context.replace('{"projects"', '')
    assert start(tmp_path) is None
    assert 'ignored' in start(tmp_path, 'sid-0002')['hookSpecificOutput']['additionalContext']


def test_autoconnect_comes_from_the_user_file_per_project(runtime: state.Runtime, tmp_path: Path) -> None:
    mine, other = tmp_path / 'mine', tmp_path / 'other'
    mine.mkdir()
    state.ensure_private_dir(state.msg_base())
    state.write_json_atomic(state.msg_base() / 'autoconnect.json', {'projects': {
        str(mine): {'hosts': [{'alias': 'gpu-a', 'host': 'u@gpu', 'path': '/srv/p'}, {'alias': '', 'host': 'x'}, 'junk']},
        str(other): {'hosts': [{'alias': 'gpu-b', 'host': 'gpu-b'}]}}})
    start(mine)
    assert state.read_json(runtime.dir / 'autoconnect.json') == [{'alias': 'gpu-a', 'host': 'u@gpu', 'path': '/srv/p'}]


@pytest.mark.parametrize('content', ['not json', '[]', '{"projects": []}', '{"projects": {"%s": {"hosts": 3}}}'])
def test_malformed_user_file_yields_no_hosts(runtime: state.Runtime, tmp_path: Path, content: str) -> None:
    state.ensure_private_dir(state.msg_base())
    (state.msg_base() / 'autoconnect.json').write_text(content.replace('%s', str(tmp_path)))
    start(tmp_path)
    assert state.read_json(runtime.dir / 'autoconnect.json') == []


#---------------------------------------------------------------------
# SessionEnd
#---------------------------------------------------------------------

def server_alive(runtime: state.Runtime) -> None:
    runtime.update_owner(server_pid=os.getpid())


@pytest.mark.parametrize('reason', ['clear', 'resume'])
def test_session_end_keeps_hosts_when_the_process_continues(runtime: state.Runtime, reason: str) -> None:
    server_alive(runtime)
    assert hooks.dispatch('SessionEnd', {'reason': reason}) is None
    assert not runtime.shutdown_path.exists()


@pytest.mark.parametrize('reason', ['logout', 'prompt_input_exit', 'other'])
def test_session_end_signals_and_returns_without_waiting(runtime: state.Runtime, reason: str) -> None:
    server_alive(runtime)
    started = time.monotonic()
    assert hooks.dispatch('SessionEnd', {'reason': reason}) is None
    assert time.monotonic() - started < 0.5
    assert runtime.shutdown_path.exists()


def test_session_end_hook_has_no_timeout() -> None:
    config = json.loads((Path(__file__).resolve().parents[2] / 'hooks' / 'hooks.json').read_text())
    [entry] = config['hooks']['SessionEnd'][0]['hooks']
    assert 'timeout' not in entry


#---------------------------------------------------------------------
# Memory index: ssh with a timeout, never the mount
#---------------------------------------------------------------------

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
    assert 'No remote memory' in host.memory_block()


def test_session_start_injects_the_cached_index_and_never_reads_the_mount(runtime: state.Runtime, tmp_path: Path) -> None:
    runtime.update_state(lambda saved: saved['hosts'].__setitem__('gpu-a', {'host': 'h', 'status': 'connected', 'mounted_mem': True}))
    mount_dir = runtime.mem_path('gpu-a')
    os.mkfifo(mount_dir / 'MEMORY.md')
    state.write_text_atomic(state.ensure_private_dir(runtime.dir / 'context') / 'gpu-a.memory', '- [Cached](c.md)\n')
    done: list[Any] = []
    thread = threading.Thread(target=lambda: done.append(start(tmp_path)), daemon=True)
    thread.start()
    thread.join(5)
    assert done, 'SessionStart blocked on the mount'
    assert '[Cached](c.md)' in done[0]['hookSpecificOutput']['additionalContext']
    assert runtime.claim_context() == [], 'the cache must not be claimed as queued context'
    (mount_dir / 'MEMORY.md').unlink()


def test_session_start_skips_a_host_without_a_cached_index(runtime: state.Runtime, tmp_path: Path) -> None:
    runtime.update_state(lambda saved: saved['hosts'].__setitem__('gpu-a', {'host': 'h', 'status': 'connected', 'mounted_mem': True}))
    assert start(tmp_path) is None


#---------------------------------------------------------------------
# Server: autoconnect on a new session id, messages across /clear
#---------------------------------------------------------------------

@pytest.fixture
def app(runtime: state.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> server.Server:
    monkeypatch.setattr(server.relay, 'remote_register', lambda master, sid, entry: None)
    monkeypatch.setattr(server.relay, 'remote_unregister', lambda master, sid, timeout=30: None)
    monkeypatch.setattr(server.relay, 'remote_cleanup', lambda master, machine, live: None)
    return server.Server(runtime, server.Config(), store=relay.Store(tmp_path / 'msg'), on_exit=lambda: None)


def wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return bool(predicate())


def test_new_session_id_connects_hosts_that_are_not_connected(app: server.Server, monkeypatch: pytest.MonkeyPatch) -> None:
    connected: list[str] = []
    monkeypatch.setattr(app, 'connect_host', lambda alias, host, path: connected.append(alias) or ('connected', False))
    app.runtime.update_owner(session_id='sid-0001', cwd='/w')
    state.write_json_atomic(app.runtime.dir / 'autoconnect.json', [{'alias': 'gpu-a', 'host': 'h'}])
    app.run_startup()
    assert wait_until(lambda: connected == ['gpu-a'])

    #--- unchanged id: nothing happens ---
    app.autoconnect_new_session()
    time.sleep(0.1)
    assert connected == ['gpu-a']

    #--- /resume into a session of another project: gpu-a is held, gpu-b is new ---
    app.hosts['gpu-a'] = SimpleNamespace(status='connected')
    state.write_json_atomic(app.runtime.dir / 'autoconnect.json', [{'alias': 'gpu-a', 'host': 'h'}, {'alias': 'gpu-b', 'host': 'h2', 'path': '/p'}])
    app.runtime.update_owner(session_id='sid-0002')
    app.autoconnect_new_session()
    assert wait_until(lambda: connected == ['gpu-a', 'gpu-b'])
    app.autoconnect_new_session()
    time.sleep(0.1)
    assert connected == ['gpu-a', 'gpu-b']


def test_clear_moves_unread_messages_and_pulls_the_old_outbox(app: server.Server, monkeypatch: pytest.MonkeyPatch) -> None:
    pulls: list[str] = []

    def pull(master: Any, sid: str, store: relay.Store, timeout: float = 30) -> int:
        pulls.append(sid)
        store.deliver(relay.new_message('remote-0001', 'peer', 'gpu-a', sid, 'late from the host'))
        return 1

    monkeypatch.setattr(server.relay, 'pull_outbox', pull)
    app.hosts = {'gpu-a': SimpleNamespace(alias='gpu-a', status='connected', master=SimpleNamespace(alias='gpu-a'))}
    app.runtime.update_owner(session_id='old-0001', cwd='/w')
    app.sync_session_id()
    app.store.deliver(relay.new_message('peer-0001', 'peer', 'here', 'old-0001', 'sent before clear'))

    app.runtime.update_owner(session_id='new-0002')
    app.sync_session_id()
    assert pulls == ['old-0001']
    assert not app.store.has_pending('old-0001')
    bodies = sorted(msg['body'] for msg in app.store.pending('new-0002'))
    assert bodies == ['late from the host', 'sent before clear']
    assert all(msg['to_session'] == 'new-0002' for msg in app.store.pending('new-0002'))


#---------------------------------------------------------------------
# Wording
#---------------------------------------------------------------------

def test_stale_wording_is_gone() -> None:
    source = Path(server.__file__).read_text()
    assert 'links rewritten' not in source and 'no longer exist' not in source
