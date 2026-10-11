"""Unit tests for remote_ssh.ident."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest

from remote_ssh import ident
from remote_ssh.ident import ClaudeProc


def test_key_is_sha1_prefix() -> None:
    proc = ClaudeProc(1234, '5678')
    assert proc.key == hashlib.sha1(b'1234:5678').hexdigest()[:8]
    assert len(proc.key) == 8


def test_proc_start_stable_for_self_and_none_for_missing() -> None:
    start = ident.proc_start(os.getpid())
    assert start is not None
    assert ident.proc_start(os.getpid()) == start
    assert ident.proc_start(2**22 + 12345) is None


def test_parent_of_returns_ppid_and_none_for_missing_pid() -> None:
    assert ident.parent_of(os.getpid()) == os.getppid()
    assert ident.parent_of(2**22 + 12345) is None


def test_alive_is_false_for_a_different_start_token() -> None:
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        proc = ClaudeProc(child.pid, ident.proc_start(child.pid))
        assert ident.alive(proc)
        assert not ident.alive(ClaudeProc(child.pid, 'other-start'))
    finally:
        child.kill()
        child.wait()
    assert not ident.alive(proc)


def test_is_claude_is_false_for_non_claude_and_missing_pids() -> None:
    assert not ident.is_claude(os.getpid())
    assert not ident.is_claude(2**22 + 12345)


@pytest.mark.parametrize(('comm', 'exe', 'argv', 'expected'), [
    ('claude', '', [], True),
    ('2.1.0', '/home/u/.local/share/claude/versions/2.1.0', ['2.1.0'], True),
    ('node', '/usr/bin/node', ['/usr/local/bin/claude', '--resume'], True),
    ('node', '/usr/bin/node', ['node', '/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js'], True),
    ('node', '/usr/bin/node', ['node', 'server.js'], False),
    ('bash', '/usr/bin/bash', ['bash', '-c', 'claude-wrapper'], False),
])
def test_looks_like_claude_matches_process_name_and_executable(comm: str, exe: str, argv: list[str], expected: bool) -> None:
    assert ident._looks_like_claude(comm, exe, argv) is expected


def test_find_claude_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('REMOTE_SSH_CLAUDE_PID', str(os.getpid()))
    assert ident.find_claude() == ClaudeProc(os.getpid(), ident.proc_start(os.getpid()))


def test_find_claude_env_override_missing_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('REMOTE_SSH_CLAUDE_PID', str(2**22 + 12345))
    assert ident.find_claude() is None


def test_find_claude_walks_parents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('REMOTE_SSH_CLAUDE_PID', raising=False)
    chain = {50: 40, 40: 30, 30: 1}
    monkeypatch.setattr(ident, 'parent_of', chain.get)
    monkeypatch.setattr(ident, 'is_claude', lambda pid: pid == 30)
    monkeypatch.setattr(ident, 'proc_start', lambda pid: f'start-{pid}')
    assert ident.find_claude(50) == ClaudeProc(30, 'start-30')


def test_find_claude_none_without_ancestor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('REMOTE_SSH_CLAUDE_PID', raising=False)
    monkeypatch.setattr(ident, 'parent_of', {50: 40, 40: 1}.get)
    monkeypatch.setattr(ident, 'is_claude', lambda pid: False)
    assert ident.find_claude(50) is None


def test_find_claude_stops_after_max_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('REMOTE_SSH_CLAUDE_PID', raising=False)
    visited = []
    monkeypatch.setattr(ident, 'parent_of', lambda pid: pid + 1)
    monkeypatch.setattr(ident, 'is_claude', lambda pid: visited.append(pid) or False)
    assert ident.find_claude(100) is None
    assert len(visited) == ident.MAX_WALK_STEPS


@pytest.mark.skipif(bool(os.environ.get('CI')), reason='needs to run under a real Claude Code process')
def test_find_claude_from_real_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('REMOTE_SSH_CLAUDE_PID', raising=False)
    proc = ident.find_claude(os.getpid())
    if proc is None:
        pytest.skip('not running under Claude Code')
    assert ident.is_claude(proc.pid)
    assert ident.alive(proc)
