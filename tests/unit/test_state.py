"""Unit tests for remote_ssh.state."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from remote_ssh import state
from remote_ssh.ident import ClaudeProc
from remote_ssh.state import Runtime, UnsafeDirError


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt'))
    return Runtime(ClaudeProc(4242, 'start'))


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


#---------------------------------------------------------------------
# Base directories
#---------------------------------------------------------------------

def test_bases_follow_env_at_call_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('REMOTE_SSH_RUNTIME_BASE', raising=False)
    monkeypatch.delenv('REMOTE_SSH_MSG_BASE', raising=False)
    assert state.runtime_base() == Path(f'/tmp/remote-ssh-{os.getuid()}')
    assert state.msg_base() == Path.home() / '.claude' / 'remote-ssh'
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'a'))
    monkeypatch.setenv('REMOTE_SSH_MSG_BASE', str(tmp_path / 'b'))
    assert state.runtime_base() == tmp_path / 'a'
    assert state.msg_base() == tmp_path / 'b'


def test_iter_runtime_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'missing'))
    assert list(state.iter_runtime_dirs()) == []
    base_root = tmp_path / 'base'
    (base_root / 'aaaa').mkdir(parents=True)
    (base_root / 'bbbb').mkdir()
    (base_root / 'file').write_text('x')
    (base_root / 'link').symlink_to(base_root / 'aaaa')
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(base_root))
    assert list(state.iter_runtime_dirs()) == [base_root / 'aaaa', base_root / 'bbbb']


#---------------------------------------------------------------------
# Validation and file primitives
#---------------------------------------------------------------------

@pytest.mark.parametrize('alias', ['gpu-a', 'a', '0123456789abcdef'])
def test_validate_alias_accepts(alias: str) -> None:
    assert state.validate_alias(alias) == alias


@pytest.mark.parametrize('alias', ['', 'GPU', '../x', 'a/b', 'a_b', '0123456789abcdefg', 'a b'])
def test_validate_alias_rejects(alias: str) -> None:
    with pytest.raises(ValueError):
        state.validate_alias(alias)


def test_ensure_private_dir_creates_0700(tmp_path: Path) -> None:
    target_dir = tmp_path / 'x' / 'y'
    assert state.ensure_private_dir(target_dir) == target_dir
    assert _mode(target_dir) == 0o700
    state.ensure_private_dir(target_dir)


def test_ensure_private_dir_rejects_open_mode(tmp_path: Path) -> None:
    target_dir = tmp_path / 'open'
    target_dir.mkdir()
    target_dir.chmod(0o755)
    with pytest.raises(UnsafeDirError):
        state.ensure_private_dir(target_dir)


def test_ensure_private_dir_rejects_symlink(tmp_path: Path) -> None:
    real_dir = tmp_path / 'real'
    real_dir.mkdir(mode=0o700)
    link_dir = tmp_path / 'link'
    link_dir.symlink_to(real_dir)
    with pytest.raises(UnsafeDirError):
        state.ensure_private_dir(link_dir)


def test_ensure_private_dir_rejects_foreign_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target_dir = tmp_path / 'mine'
    target_dir.mkdir(mode=0o700)
    monkeypatch.setattr(state.os, 'getuid', lambda: os.lstat(target_dir).st_uid + 1)
    with pytest.raises(UnsafeDirError):
        state.ensure_private_dir(target_dir)


def test_write_and_read_json(tmp_path: Path) -> None:
    json_path = tmp_path / 'a.json'
    state.write_json_atomic(json_path, {'k': [1, 2]})
    assert state.read_json(json_path) == {'k': [1, 2]}
    assert _mode(json_path) == 0o600
    state.write_json_atomic(json_path, {'k': 3})
    assert state.read_json(json_path) == {'k': 3}
    assert [entry.name for entry in tmp_path.iterdir()] == ['a.json']


def test_read_json_default(tmp_path: Path) -> None:
    assert state.read_json(tmp_path / 'missing.json', {'d': 1}) == {'d': 1}
    bad_path = tmp_path / 'bad.json'
    bad_path.write_text('{not json')
    assert state.read_json(bad_path) is None


#---------------------------------------------------------------------
# Runtime directory
#---------------------------------------------------------------------

def test_runtime_dir_is_private(runtime: Runtime, tmp_path: Path) -> None:
    assert runtime.dir == tmp_path / 'rt' / runtime.claude.key
    assert _mode(runtime.dir) == 0o700
    assert _mode(runtime.dir.parent) == 0o700
    assert runtime.owner_path == runtime.dir / 'owner.json'
    assert runtime.state_path == runtime.dir / 'state.json'
    assert runtime.shutdown_path == runtime.dir / 'shutdown'
    assert runtime.lock_path == runtime.dir / 'state.lock'


def test_runtime_rejects_open_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_root = tmp_path / 'rt'
    base_root.mkdir(mode=0o777)
    base_root.chmod(0o777)
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(base_root))
    with pytest.raises(UnsafeDirError):
        Runtime(ClaudeProc(1, 's'))


def test_runtime_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('REMOTE_SSH_RUNTIME_BASE', str(tmp_path / 'rt'))
    monkeypatch.setattr(state, 'find_claude', lambda: ClaudeProc(7, 's'))
    assert Runtime.current().claude == ClaudeProc(7, 's')
    monkeypatch.setattr(state, 'find_claude', lambda: None)
    assert Runtime.current() is None


def test_host_paths(runtime: Runtime) -> None:
    host_dir = runtime.host_dir('gpu-a')
    assert host_dir == runtime.dir / 'gpu-a'
    assert _mode(host_dir) == 0o700
    assert runtime.ctl_path('gpu-a') == host_dir / 'c'
    assert runtime.fs_path('gpu-a').is_dir()
    assert runtime.fs_path('gpu-a') == host_dir / 'fs'
    assert runtime.mem_path('gpu-a').is_dir()
    assert runtime.mem_path('gpu-a') == host_dir / 'mem'
    with pytest.raises(ValueError):
        runtime.host_dir('../escape')


def test_owner_merge(runtime: Runtime) -> None:
    assert runtime.owner() == {}
    runtime.update_owner(session_id='abc', cwd='/w')
    runtime.update_owner(server_pid=99)
    assert runtime.owner() == {'session_id': 'abc', 'cwd': '/w', 'server_pid': 99}


def test_load_state_defaults_are_independent(runtime: Runtime) -> None:
    first = runtime.load_state()
    first['hosts']['x'] = {}
    assert runtime.load_state() == state.DEFAULT_STATE
    assert state.DEFAULT_STATE['hosts'] == {}


def test_update_state_persists(runtime: Runtime) -> None:
    def set_redirect(current: dict) -> None:
        current['redirect'] = 'gpu-a'
        current['hosts']['gpu-a'] = {'status': 'connected'}

    saved = runtime.update_state(set_redirect)
    assert saved['redirect'] == 'gpu-a'
    assert runtime.load_state() == {**state.DEFAULT_STATE, 'redirect': 'gpu-a', 'hosts': {'gpu-a': {'status': 'connected'}}}


def test_update_state_serialises_concurrent_updates(runtime: Runtime) -> None:
    def increment(current: dict) -> None:
        current['count'] = current.get('count', 0) + 1

    workers = [threading.Thread(target=lambda: [runtime.update_state(increment) for _ in range(20)]) for _ in range(5)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert runtime.load_state()['count'] == 100
