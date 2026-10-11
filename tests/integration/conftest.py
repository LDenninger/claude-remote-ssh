"""Fixtures that run the plugin against an unprivileged sshd on 127.0.0.1.

The remote side of every test is this machine as the current user, so its HOME is the real
one: every remote path a test uses lives under `tmp_path`, and `REMOTE_SSH_REMOTE_BASE`
moves the remote message store under `tmp_path` too.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from remote_ssh import ident, mount

ROOT = Path(__file__).resolve().parents[2]
SSHD = shutil.which('sshd') or '/usr/sbin/sshd'
REPLY_TIMEOUT_S = 90.0


def _free_port() -> int:
    """Return a TCP port on 127.0.0.1 that was free a moment ago."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture(scope='session')
def sshd(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Start sshd as the current user and return the ssh options that reach it."""
    if not os.path.exists(SSHD):
        pytest.skip('sshd not installed')
    sshd_dir = tmp_path_factory.mktemp('sshd')
    for name in ('hostkey', 'client'):
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(sshd_dir / name)], check=True)
    shutil.copy(sshd_dir / 'client.pub', sshd_dir / 'authorized_keys')
    port = _free_port()
    (sshd_dir / 'cfg').write_text(
        f'Port {port}\nListenAddress 127.0.0.1\nHostKey {sshd_dir / "hostkey"}\n'
        f'AuthorizedKeysFile {sshd_dir / "authorized_keys"}\nPidFile {sshd_dir / "pid"}\nUsePAM no\nStrictModes no\n'
        'PasswordAuthentication no\nKbdInteractiveAuthentication no\nSubsystem sftp internal-sftp\n'
    )
    proc = subprocess.Popen([SSHD, '-D', '-e', '-f', str(sshd_dir / 'cfg')], stderr=open(sshd_dir / 'sshd.log', 'w'))
    opts = (
        f'-F /dev/null -p {port} -i {sshd_dir / "client"} -o IdentitiesOnly=yes '
        f'-o UserKnownHostsFile={sshd_dir / "known_hosts"} -o StrictHostKeyChecking=accept-new'
    )
    for _ in range(50):
        probe = subprocess.run(['ssh', *shlex.split(opts), '-o', 'BatchMode=yes', '127.0.0.1', 'true'],
                               stdin=subprocess.DEVNULL, capture_output=True)
        if probe.returncode == 0:
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail('sshd did not come up: ' + (sshd_dir / 'sshd.log').read_text())
    yield {'host': '127.0.0.1', 'opts': opts, 'dir': sshd_dir}
    proc.terminate()
    proc.wait(5)


#---------------------------------------------------------------------
# MCP client and hook runner
#---------------------------------------------------------------------

class Mcp:
    """JSON-RPC client for the plugin's stdio MCP server.

    A reader thread collects replies by id, so several requests can be in flight at
    once and a hung server fails the test after `REPLY_TIMEOUT_S` instead of blocking it.

    Args:
        env: Environment of the server process.
        argv: Command line of the server. Defaults to `python scripts/server.py`.
    """

    def __init__(self, env: dict[str, str], argv: list[str] | None = None) -> None:
        argv = argv or [sys.executable, str(ROOT / 'scripts' / 'server.py')]
        self.proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self._id = 0
        self._replies: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self.__read_replies, daemon=True).start()
        self.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '0'}})
        self.notify('notifications/initialized')

    def __read_replies(self) -> None:
        """Route every reply line to the queue of its request id."""
        for line in self.proc.stdout:
            msg = json.loads(line)
            with self._lock:
                self._replies.setdefault(msg.get('id'), queue.Queue()).put(msg)

    def notify(self, method: str, params: dict | None = None) -> None:
        """Send a notification, which gets no reply."""
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params or {}}) + '\n')
        self.proc.stdin.flush()

    def send(self, method: str, params: dict | None = None) -> int:
        """Send a request without waiting and return its id."""
        with self._lock:
            self._id += 1
            msg_id = self._id
            self._replies.setdefault(msg_id, queue.Queue())
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'method': method, 'params': params or {}}) + '\n')
        self.proc.stdin.flush()
        return msg_id

    def wait(self, msg_id: int, timeout: float = REPLY_TIMEOUT_S) -> dict:
        """Return the reply to request `msg_id`, failing the test after `timeout` seconds."""
        with self._lock:
            replies = self._replies.setdefault(msg_id, queue.Queue())
        try:
            return replies.get(timeout=timeout)
        except queue.Empty:
            pytest.fail(f'no reply to request {msg_id} within {timeout} s')

    def request(self, method: str, params: dict | None = None) -> dict:
        """Send a request and return its reply."""
        return self.wait(self.send(method, params))

    def call(self, tool: str, **args: Any) -> tuple[str, bool]:
        """Call one tool and return its text and error flag."""
        return self.result(self.request('tools/call', {'name': tool, 'arguments': args}))

    @staticmethod
    def result(reply: dict) -> tuple[str, bool]:
        """Return the text and error flag of a `tools/call` reply."""
        res = reply['result']
        return res['content'][0]['text'], res.get('isError', False)

    def close(self, timeout: float = 30) -> int:
        """Close stdin, which tears the server down, and return its exit code."""
        self.proc.stdin.close()
        return self.proc.wait(timeout)


def run_hook(env: dict[str, str], event: str, payload: dict) -> dict:
    """Run one hook event through scripts/hook.py and return its parsed output, {} for none."""
    out = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'hook.py'), event], env=env,
                         input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout) if out.stdout.strip() else {}


#---------------------------------------------------------------------
# Probes
#---------------------------------------------------------------------

def wait_for(predicate: Callable[[], Any], timeout: float, interval: float = 0.2) -> Any:
    """Poll `predicate` until it returns a truthy value or `timeout` seconds pass.

    Returns:
        The truthy value, or the last falsy one on timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def pids_matching(pattern: str) -> list[int]:
    """Return the pids whose full command line contains `pattern` literally."""
    out = subprocess.run(['pgrep', '-f', re.escape(pattern)], capture_output=True, text=True)
    return [int(pid) for pid in out.stdout.split()]


def is_running(pid: int) -> bool:
    """Return whether `pid` exists and is not a zombie."""
    try:
        stat_text = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return False
    return stat_text[stat_text.rfind(')') + 2] != 'Z'


def runtime_dir_of(rt_root: Path, claude_pid: int) -> Path:
    """Return the runtime directory the plugin uses for the Claude process `claude_pid`."""
    return rt_root / ident.ClaudeProc(claude_pid, ident.proc_start(claude_pid)).key


def release_leftovers(rt_root: Path) -> None:
    """Unmount and kill whatever a failed test left under `rt_root`, so tmp cleanup is safe."""
    if not rt_root.is_dir():
        return
    for mountpoint in list(rt_root.glob('*/*/fs')) + list(rt_root.glob('*/*/mem')):
        mount.unmount(mountpoint)
    for pid in pids_matching(str(rt_root)):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


#---------------------------------------------------------------------
# Sessions
#---------------------------------------------------------------------

class Session:
    """One plugin session: a stand-in Claude process, its hooks' env and its MCP server.

    Attributes:
        env: Environment shared by the server and the hooks of this session.
        mcp: Client of the running server.
        sid: Session id written by the SessionStart hook.
        cwd: Local working directory of the session.
        stand_in: The process playing the Claude process.
        rt: Runtime directory of the session.
    """

    def __init__(self, env: dict[str, str], mcp: Mcp, sid: str, cwd: Path, stand_in: subprocess.Popen, rt: Path) -> None:
        self.env = env
        self.mcp = mcp
        self.sid = sid
        self.cwd = cwd
        self.stand_in = stand_in
        self.rt = rt


def base_env(sshd: dict, tmp_path: Path, name: str) -> dict[str, str]:
    """Return the plugin env of session `name`, without the Claude pid."""
    return {**os.environ,
            'REMOTE_SSH_RUNTIME_BASE': str(tmp_path / 'rt'),
            'REMOTE_SSH_MSG_BASE': str(tmp_path / f'msg-{name}'),
            'REMOTE_SSH_REMOTE_BASE': str(tmp_path / 'remote-msg'),
            'REMOTE_SSH_SSH_OPTS': sshd['opts'],
            'REMOTE_SSH_POLL_INTERVAL': '1'}


def start_session_hook(env: dict[str, str], name: str, cwd: Path) -> str:
    """Run SessionStart for session `name` and return its session id."""
    sid = f'{name}-0000-0000-0000-000000000000'
    run_hook(env, 'SessionStart', {'session_id': sid, 'cwd': str(cwd), 'hook_event_name': 'SessionStart', 'source': 'startup'})
    return sid


@pytest.fixture
def session(sshd: dict, tmp_path: Path) -> Any:
    """Yield a factory for isolated plugin sessions, each with its own stand-in Claude process.

    The factory takes a name and optional overrides of the session env, e.g. a message base.
    """
    made: list[Session] = []

    def make(name: str, cwd: Path | None = None, **env_overrides: str) -> Session:
        stand_in = subprocess.Popen(['sleep', '3600'])
        cwd = cwd or tmp_path / name
        cwd.mkdir(parents=True, exist_ok=True)
        env = {**base_env(sshd, tmp_path, name), **env_overrides, 'REMOTE_SSH_CLAUDE_PID': str(stand_in.pid)}
        sid = start_session_hook(env, name, cwd)
        made.append(Session(env, Mcp(env), sid, cwd, stand_in, runtime_dir_of(tmp_path / 'rt', stand_in.pid)))
        return made[-1]

    yield make
    for sess in made:
        if sess.mcp.proc.poll() is None:
            sess.mcp.close()
        sess.stand_in.kill()
        sess.stand_in.wait()
    release_leftovers(tmp_path / 'rt')


@pytest.fixture
def remote_dir(tmp_path: Path) -> Path:
    """Return a fresh "remote" project directory, resolved as the remote shell resolves it."""
    path = (tmp_path / 'remote-proj').resolve()
    path.mkdir()
    return path
