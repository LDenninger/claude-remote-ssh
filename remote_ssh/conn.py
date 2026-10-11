"""SSH ControlMaster connections to remote hosts.

A `Master` owns one multiplexed ssh connection per host alias. Every later command, sshfs mount
and relay round trip reuses its control socket instead of authenticating again. `exec_argv` builds
the argv for one command over that socket, and `remote_shell` quotes the command so that the remote
shell runs it unchanged. User commands run in a `bash -l` login shell, plugin plumbing in plain
`sh`, so polling never sources the user's login profile.
"""

from __future__ import annotations

import shlex
import socket
import subprocess
import tempfile
import time
from pathlib import Path

TRUNCATE = 30_000
KILL_AFTER_S = 5
EXEC_GRACE_S = 15


class ConnectError(Exception):
    """The ControlMaster could not be established."""


#---------------------------------------------------------------------
# Command construction
#---------------------------------------------------------------------

def expand_remote_path(path: str) -> str:
    """Return a shell word that `cd` resolves relative to the remote home.

    Args:
        path: Requested remote path. `~` and `~/x` become `.` and `x`, for a shell that has
            changed into the remote home first. Anything else is quoted unchanged.
    """
    if path in ('~', '~/'):
        return '.'
    if path.startswith('~/'):
        return shlex.quote(path[2:])
    return shlex.quote(path)


def remote_shell(command: str, cwd: str | None, login: bool = True) -> str:
    """Wrap a command for the remote shell.

    Args:
        command: Shell command to run remotely, passed through verbatim.
        cwd: Remote directory to change into first, `~`-relative paths resolved against the
            remote home. None runs in the login directory.
        login: Run under `bash -lc`, sourcing the login profile, else under plain `sh -c`.
            Defaults to True.

    Returns:
        A single string for ssh's remote command.
    """
    if cwd and (cwd == '~' or cwd.startswith('~/')):
        command = f'cd ~ && cd -- {expand_remote_path(cwd)} && {command}'
    elif cwd:
        command = f'cd -- {shlex.quote(cwd)} && {command}'
    return f'{"bash -lc" if login else "sh -c"} {shlex.quote(command)}'


def with_remote_timeout(remote: str, seconds: int) -> str:
    """Wrap a remote command line so the remote `timeout` ends it after `seconds`.

    Killing the local ssh client leaves the remote command running, so the limit is enforced
    on the remote. GNU `timeout` signals the command's whole process group, and `KILL_AFTER_S`
    later sends SIGKILL. A remote without `timeout` runs the command unlimited.
    """
    inner = 'if command -v timeout >/dev/null 2>&1; then exec timeout -k ' + str(KILL_AFTER_S) + ' "$0" sh -c "$1"; fi; exec sh -c "$1"'
    return f'sh -c {shlex.quote(inner)} {int(seconds)} {shlex.quote(remote)}'


def exec_argv(ctl: Path, host: str, command: str, cwd: str | None, extra: list[str] | None = None,
              login: bool = True, remote_timeout: int | None = None) -> list[str]:
    """Build the local argv that runs one command over an existing control socket.

    Args:
        ctl: Control socket of the running master.
        host: ssh destination the master was started for.
        command: Shell command to run remotely.
        cwd: Remote working directory, or None for the login directory.
        extra: Additional ssh options inserted before `-T`. Defaults to none.
        login: Run in a `bash -l` login shell, else in plain `sh`. Defaults to True.
        remote_timeout: Seconds after which the remote ends the command. Defaults to no limit.

    Returns:
        The ssh argv, ready for `subprocess.run` or `shlex.join`.
    """
    remote = remote_shell(command, cwd, login)
    if remote_timeout:
        remote = with_remote_timeout(remote, remote_timeout)
    return ['ssh', '-S', str(ctl), '-o', 'BatchMode=yes', *(extra or []), '-T', '--', host, remote]


def tail(s: str, n: int = TRUNCATE) -> str:
    """Keep the last `n` characters of a string.

    Args:
        s: Text to shorten.
        n: Maximum number of characters kept. Defaults to `TRUNCATE`.

    Returns:
        `s` unchanged if it fits, else its last `n` characters prefixed with `[truncated]`.
    """
    if len(s) <= n:
        return s
    return '[truncated]\n' + s[-n:]


#---------------------------------------------------------------------
# ControlMaster
#---------------------------------------------------------------------

class Master:
    """One ssh ControlMaster process for a host alias.

    Args:
        alias: Local name of the host connection.
        host: ssh destination, as accepted by `ssh`.
        ctl: Path of the control socket.
        extra_ssh_opts: Additional ssh options for every invocation. Defaults to none.

    Attributes:
        alias: Local name of the host connection.
        host: ssh destination.
        ctl: Path of the control socket.
        extra: Additional ssh options, possibly empty.
    """

    def __init__(self, alias: str, host: str, ctl: Path, extra_ssh_opts: list[str] | None = None) -> None:
        self.alias = alias
        self.host = host
        self.ctl = Path(ctl)
        self.extra = list(extra_ssh_opts or [])
        self._proc: subprocess.Popen | None = None

    #---------------------------------------------------------------------
    # Lifecycle
    #---------------------------------------------------------------------

    def start(self, timeout: float = 20) -> None:
        """Start the master and wait until its control socket answers.

        Args:
            timeout: Seconds to wait for the socket. Defaults to 20.

        Raises:
            ConnectError: ssh exited early or the socket did not answer in time. The message holds
                ssh's stderr.
        """
        argv = [
            'ssh', '-M', '-N', '-S', str(self.ctl),
            '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-o', 'ControlPersist=no',
            *self.extra, '--', self.host,
        ]
        with tempfile.TemporaryFile() as err_file:
            self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err_file)
            deadline = time.monotonic() + timeout
            while True:
                if self.check():
                    return
                if self._proc.poll() is not None or time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
            self.stop()
            err_file.seek(0)
            message = err_file.read().decode('utf-8', errors='replace').strip()
        raise ConnectError(message or f'ssh master for {self.host} not ready after {timeout}s')

    def check(self) -> bool:
        """Return whether the control socket answers `ssh -O check`."""
        if not self.ctl.exists():
            return False
        argv = ['ssh', '-S', str(self.ctl), *self.extra, '-O', 'check', '--', self.host]
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def is_listening(self) -> bool:
        """Return whether a process accepts connections on the control socket.

        A local connect only, without starting ssh. A master killed without cleanup leaves
        its socket file behind, which then refuses connections.
        """
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            try:
                sock.connect(str(self.ctl))
            except OSError:
                return False
        return True

    def stop(self) -> None:
        """Close the master, kill its process if needed and remove the socket. Never raises."""
        try:
            if self.ctl.exists():
                subprocess.run(['ssh', '-S', str(self.ctl), *self.extra, '-O', 'exit', '--', self.host],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        try:
            self.ctl.unlink()
        except OSError:
            pass

    #---------------------------------------------------------------------
    # Commands
    #---------------------------------------------------------------------

    def run(self, command: str, cwd: str | None = None, input: bytes | None = None, timeout: float | None = 60,
            login: bool = False, remote_timeout: int | None = None) -> subprocess.CompletedProcess:
        """Run a remote command over the master and capture raw output.

        Args:
            command: Shell command to run remotely.
            cwd: Remote working directory. Defaults to the login directory.
            input: Bytes fed to the remote stdin. Defaults to an empty stdin.
            timeout: Seconds before `subprocess.TimeoutExpired`. Defaults to 60, None waits forever.
            login: Run in a `bash -l` login shell. Defaults to False, plain `sh` for plumbing.
            remote_timeout: Seconds after which the remote ends the command. Defaults to no limit.

        Returns:
            The completed process with bytes stdout and stderr.

        Raises:
            subprocess.TimeoutExpired: The command outlived `timeout`.
        """
        argv = exec_argv(self.ctl, self.host, command, cwd, extra=self.extra, login=login, remote_timeout=remote_timeout)
        #--- never inherit our stdin: for the MCP server it is the JSON-RPC pipe ---
        stdin = None if input is not None else subprocess.DEVNULL
        return subprocess.run(argv, input=input, stdin=stdin, capture_output=True, timeout=timeout)

    def exec(self, command: str, cwd: str | None = None, timeout: int = 600) -> tuple[str, str, int]:
        """Run a remote command in a login shell and return decoded, truncated output.

        Args:
            command: Shell command to run remotely.
            cwd: Remote working directory. Defaults to the login directory.
            timeout: Seconds before the remote ends the command. The local ssh gets
                `EXEC_GRACE_S` more before it is abandoned. Defaults to 600.

        Returns:
            stdout: Decoded stdout, cut to its last `TRUNCATE` characters.
            stderr: Decoded stderr, cut the same way.
            exit code: The remote exit status, 124 on timeout.
        """
        try:
            result = self.run(command, cwd=cwd, timeout=timeout + EXEC_GRACE_S, login=True, remote_timeout=timeout)
        except subprocess.TimeoutExpired:
            return '', f'timed out after {timeout}s', 124
        stdout = tail(result.stdout.decode('utf-8', errors='replace'))
        stderr = tail(result.stderr.decode('utf-8', errors='replace'))
        if result.returncode == 124:
            stderr += f'\n[exit 124: timed out after {timeout}s, or the command itself returned 124]'
        return stdout, stderr, result.returncode

    def ssh_command(self) -> str:
        """Return the ssh command line that reuses this master, for sshfs `ssh_command`."""
        return f'ssh -S {self.ctl} -o BatchMode=yes'
