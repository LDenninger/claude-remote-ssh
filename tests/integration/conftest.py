"""Fixtures that run the plugin against an unprivileged sshd on 127.0.0.1."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SSHD = shutil.which("sshd") or "/usr/sbin/sshd"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def sshd(tmp_path_factory):
    """Start sshd as the current user and return the ssh options that reach it."""
    if not os.path.exists(SSHD):
        pytest.skip("sshd not installed")
    d = tmp_path_factory.mktemp("sshd")
    for name in ("hostkey", "client"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / name)], check=True)
    shutil.copy(d / "client.pub", d / "authorized_keys")
    port = _free_port()
    (d / "cfg").write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d / 'hostkey'}\n"
        f"AuthorizedKeysFile {d / 'authorized_keys'}\nPidFile {d / 'pid'}\nUsePAM no\nStrictModes no\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
    )
    proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(d / "cfg")], stderr=open(d / "sshd.log", "w"))
    opts = (
        f"-F /dev/null -p {port} -i {d / 'client'} -o IdentitiesOnly=yes "
        f"-o UserKnownHostsFile={d / 'known_hosts'} -o StrictHostKeyChecking=accept-new"
    )
    for _ in range(50):
        ok = subprocess.run(["ssh", *shlex.split(opts), "-o", "BatchMode=yes", "127.0.0.1", "true"],
                            stdin=subprocess.DEVNULL, capture_output=True).returncode == 0
        if ok:
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("sshd did not come up: " + (d / "sshd.log").read_text())
    yield {"host": "127.0.0.1", "opts": opts, "dir": d}
    proc.terminate()
    proc.wait(5)


class Mcp:
    """Minimal JSON-RPC client for the plugin's stdio MCP server."""

    def __init__(self, env: dict):
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "server.py")], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self._id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}})
        self.notify("notifications/initialized")

    def notify(self, method: str, params: dict | None = None) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method,
                                          "params": params or {}}) + "\n")
        while True:
            msg = json.loads(self.proc.stdout.readline())
            if msg.get("id") == self._id:
                return msg

    def call(self, tool: str, **args) -> tuple[str, bool]:
        res = self.request("tools/call", {"name": tool, "arguments": args})["result"]
        return res["content"][0]["text"], res.get("isError", False)

    def close(self, timeout: float = 15) -> int:
        self.proc.stdin.close()
        return self.proc.wait(timeout)


def run_hook(env: dict, event: str, payload: dict) -> dict:
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "hook.py"), event], env=env,
                         input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout) if out.stdout.strip() else {}


@pytest.fixture
def session(sshd, tmp_path):
    """Yield a factory for isolated plugin sessions, each with its own stand-in Claude process."""
    made = []

    def make(name: str, cwd: Path | None = None):
        stand_in = subprocess.Popen(["sleep", "3600"])
        cwd = cwd or tmp_path / name
        cwd.mkdir(parents=True, exist_ok=True)
        env = {**os.environ,
               "REMOTE_SSH_CLAUDE_PID": str(stand_in.pid),
               "REMOTE_SSH_RUNTIME_BASE": str(tmp_path / "rt"),
               "REMOTE_SSH_MSG_BASE": str(tmp_path / f"msg-{name}"),
               "REMOTE_SSH_REMOTE_BASE": str(tmp_path / "remote-msg"),
               "REMOTE_SSH_SSH_OPTS": sshd["opts"],
               "REMOTE_SSH_POLL_INTERVAL": "1"}
        sid = f"{name}-0000-0000-0000-000000000000"
        run_hook(env, "SessionStart", {"session_id": sid, "cwd": str(cwd), "hook_event_name": "SessionStart",
                                       "source": "startup"})
        mcp = Mcp(env)
        s = type("S", (), {"env": env, "mcp": mcp, "sid": sid, "cwd": cwd, "stand_in": stand_in})
        made.append(s)
        return s

    yield make
    for s in made:
        if s.mcp.proc.poll() is None:
            s.mcp.close()
        s.stand_in.kill()
