"""Unit tests for remote_ssh.conn: argv construction, quoting and the master failure path."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from remote_ssh.conn import TRUNCATE, ConnectError, Master, exec_argv, expand_remote_path, remote_shell, tail, with_remote_timeout

TRICKY = [
    'echo plain',
    "printf '%s' \"double \\\"quoted\\\" and 'single'\"",
    'printf %s "$HOME"',
    "printf '%s' 'a;b' ; printf '%s' '|c&&d'",
    "printf 'line1\\nline2'",
    'x=1\ny=2\nprintf %s "$x$y"',
    "printf '%s' '$(not run) `nor this`'",
]


def run_locally(remote: str, home: Path) -> str:
    """Execute a remote-shell string the way sshd does, `bash -c <string>`, and return stdout."""
    env = {'PATH': os.environ['PATH'], 'HOME': str(home)}
    result = subprocess.run(['bash', '-c', remote], capture_output=True, env=env, timeout=10, check=True)
    return result.stdout.decode()


@pytest.mark.parametrize('command', TRICKY)
def test_remote_shell_runs_command_unchanged(command: str, tmp_path: Path) -> None:
    expected = subprocess.run(['bash', '-c', command], capture_output=True, env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path)},
                              timeout=10, check=True).stdout.decode()
    assert run_locally(remote_shell(command, None), tmp_path) == expected


def test_remote_shell_changes_into_cwd_with_odd_characters(tmp_path: Path) -> None:
    cwd = tmp_path / "it's a $dir; with\nnewline"
    cwd.mkdir()
    assert run_locally(remote_shell('pwd', str(cwd)), tmp_path).strip() == str(cwd)


def test_remote_shell_cwd_failure_skips_command(tmp_path: Path) -> None:
    remote = remote_shell('echo ran', str(tmp_path / 'missing'))
    result = subprocess.run(['bash', '-c', remote], capture_output=True, env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path)})
    assert result.returncode != 0
    assert b'ran' not in result.stdout


def test_exec_argv_layout() -> None:
    argv = exec_argv(Path('/run/c'), 'gpu', 'ls', '/srv', extra=['-p', '2222'])
    assert argv == ['ssh', '-S', '/run/c', '-o', 'BatchMode=yes', '-p', '2222', '-T', '--', 'gpu', remote_shell('ls', '/srv')]
    assert exec_argv(Path('/run/c'), 'gpu', 'ls', None)[:7] == ['ssh', '-S', '/run/c', '-o', 'BatchMode=yes', '-T', '--']


def test_master_argv_end_options_before_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, b'', b''))
    ctl = tmp_path / 'c'
    ctl.touch()  # an existing control path, so check and stop reach ssh
    master = Master('a', 'gpu', ctl)
    master.run('true')
    master.check()
    master.stop()
    assert len(calls) == 3
    for argv in calls:
        assert argv[argv.index('gpu') - 1] == '--', argv


def test_expand_remote_path() -> None:
    assert expand_remote_path('~') == '.'
    assert expand_remote_path('~/my proj') == "'my proj'"
    assert expand_remote_path('/srv/x') == '/srv/x'


def test_remote_shell_resolves_tilde_cwd_against_home(tmp_path: Path) -> None:
    (tmp_path / 'my sub').mkdir()
    assert run_locally(remote_shell('pwd', '~/my sub'), tmp_path).strip() == str(tmp_path / 'my sub')
    assert run_locally(remote_shell('pwd', '~'), tmp_path).strip() == str(tmp_path)


def test_plumbing_shell_skips_login_profile(tmp_path: Path) -> None:
    (tmp_path / '.bash_profile').write_text('echo PROFILE-SOURCED\n')
    assert 'PROFILE-SOURCED' in run_locally(remote_shell('true', None), tmp_path)
    assert run_locally(remote_shell('echo plain', None, login=False), tmp_path) == 'plain\n'
    assert exec_argv(Path('/run/c'), 'gpu', 'true', None, login=False)[-1].startswith('sh -c ')


def test_run_defaults_to_plain_shell_and_exec_to_login_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, b'', b''))
    master = Master('a', 'gpu', Path('/run/c'))
    master.run('true')
    master.exec('true', timeout=7)
    assert calls[0][-1].startswith('sh -c ')
    assert 'bash -lc' in calls[1][-1] and 'timeout -k' in calls[1][-1] and ' 7 ' in calls[1][-1]


def test_remote_timeout_ends_the_command(tmp_path: Path) -> None:
    started = time.monotonic()
    remote = with_remote_timeout(remote_shell('sleep 30', None), 1)
    result = subprocess.run(['bash', '-c', remote], capture_output=True, env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path)}, timeout=20)
    assert result.returncode == 124
    assert time.monotonic() - started < 10


def test_tail() -> None:
    assert tail('abc') == 'abc'
    assert tail('x' * TRUNCATE) == 'x' * TRUNCATE
    assert tail('abcdef', n=3) == '[truncated]\ndef'


def test_ssh_command() -> None:
    assert Master('a', 'h', Path('/run/c')).ssh_command() == 'ssh -S /run/c -o BatchMode=yes'


def test_start_fails_fast_on_refused_port(tmp_path: Path) -> None:
    ctl = tmp_path / 'c'
    master = Master('dead', '127.0.0.1', ctl, ['-F', '/dev/null', '-p', '1', '-o', 'ConnectTimeout=1'])
    with pytest.raises(ConnectError) as info:
        master.start(timeout=10)
    assert str(info.value)
    assert not ctl.exists()
    assert master.check() is False
    master.stop()


def test_exec_timeout_returns_124(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired('ssh', 5)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert Master('a', 'h', Path('/run/c')).exec('sleep 9', timeout=5) == ('', 'timed out after 5s', 124)


def test_run_never_inherits_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: calls.append(kwargs) or subprocess.CompletedProcess(argv, 0, b'', b''))
    master = Master('a', 'h', Path('/run/c'))
    master.run('true')
    master.run('cat', input=b'data')
    assert calls[0]['stdin'] is subprocess.DEVNULL
    assert calls[1]['stdin'] is None and calls[1]['input'] == b'data'


def test_is_listening_tells_live_from_stale_socket(tmp_path: Path) -> None:
    ctl = tmp_path / 'c'
    master = Master('h', 'host', ctl)
    assert not master.is_listening()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(ctl))
    listener.listen()
    assert master.is_listening()
    listener.close()
    assert ctl.exists()
    assert not master.is_listening()
