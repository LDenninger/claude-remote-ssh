"""sshfs mounts over an existing ControlMaster.

`mount` attaches a remote directory at a local mountpoint, reusing the master's control socket so
sshfs never authenticates on its own. `unmount` detaches it and falls back to a lazy or forced
unmount when the normal one fails or hangs, so teardown never blocks on a dead connection.
"""

from __future__ import annotations

import errno
import os
import re
import subprocess
from pathlib import Path

from remote_ssh import platform
from remote_ssh.conn import Master


class MountError(Exception):
    """sshfs failed to mount a remote directory."""


#---------------------------------------------------------------------
# Mounting
#---------------------------------------------------------------------

def ssh_command_option(master: Master) -> str:
    """Return the sshfs `ssh_command` value that runs ssh through the master.

    sshfs splits the value on spaces and honours backslash escapes, not shell quoting, and a
    comma would end the enclosing `-o` value. Spaces and backslashes are therefore escaped
    with a backslash.

    Args:
        master: Running master whose ssh argv is joined.

    Returns:
        The escaped command line.

    Raises:
        MountError: An argument contains a comma, which sshfs cannot carry in `ssh_command`.
    """
    words = []
    for word in master.ssh_argv():
        if ',' in word:
            raise MountError(f'ssh argument {word!r} contains a comma, which sshfs cannot pass in ssh_command; '
                             'use a path or option without one')
        words.append(re.sub(r'([ \\])', r'\\\1', word))
    return ' '.join(words)


def sshfs_argv(master: Master, remote_path: str, mountpoint: Path, extra_opts: str = '') -> list[str]:
    """Build the sshfs argv that mounts a remote directory through the master.

    Args:
        master: Running master whose control socket sshfs reuses.
        remote_path: Absolute directory on the remote host.
        mountpoint: Local, existing directory to mount onto.
        extra_opts: Comma-separated sshfs options appended as one more `-o`. Defaults to none.

    Returns:
        The sshfs argv.

    Raises:
        MountError: The ssh arguments cannot be expressed in `ssh_command`, see `ssh_command_option`.
    """
    ssh_command = ssh_command_option(master)
    argv = ['sshfs', f'{master.host}:{remote_path}', str(mountpoint), '-o', f'ssh_command={ssh_command}', '-o', 'reconnect,ServerAliveInterval=15']
    if platform.IS_MAC:
        argv += ['-o', f'volname={master.alias},noappledouble']
    if extra_opts:
        argv += ['-o', extra_opts]
    return argv


def mount(master: Master, remote_path: str, mountpoint: Path, extra_opts: str = '', timeout: float = 20) -> None:
    """Mount a remote directory at a local mountpoint.

    Args:
        master: Running master whose control socket sshfs reuses.
        remote_path: Absolute directory on the remote host.
        mountpoint: Local directory to mount onto, created if missing.
        extra_opts: Comma-separated sshfs options. Defaults to none.
        timeout: Seconds sshfs may take to mount. Defaults to 20.

    Raises:
        MountError: sshfs exited non-zero, timed out or is not installed. The message holds its
            stderr.
    """
    mountpoint.mkdir(parents=True, exist_ok=True)
    argv = sshfs_argv(master, remote_path, mountpoint, extra_opts)
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MountError(f'sshfs timed out after {timeout}s mounting {remote_path}') from exc
    except OSError as exc:
        raise MountError(f'sshfs could not start: {exc}') from exc
    if result.returncode != 0:
        message = result.stderr.decode('utf-8', errors='replace').strip()
        raise MountError(message or f'sshfs exited with {result.returncode}')


#---------------------------------------------------------------------
# Unmounting
#---------------------------------------------------------------------

MOUNTINFO = Path('/proc/self/mountinfo')
DEAD_MOUNT_ERRNOS = {errno.ENOTCONN, errno.EIO, errno.ENOENT}


def is_mounted(mountpoint: Path) -> bool:
    """Return whether a filesystem is mounted at `mountpoint`, including a dead FUSE mount.

    Args:
        mountpoint: Local mountpoint to check. Only its parent is resolved, since resolving a hung
            mount itself would block.

    Returns:
        True if the kernel mount table lists `mountpoint` (Linux), or, elsewhere, if `lstat` fails
        with a dead-mount error while the parent still lists it. `os.path.ismount` alone misses dead
        FUSE mounts because their `lstat` fails.
    """
    if MOUNTINFO.exists():
        absolute = os.path.abspath(mountpoint)
        target = os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))
        for line in MOUNTINFO.read_text().splitlines():
            field = line.split(' ')[4]
            if re.sub(r'\\(\d{3})', lambda m: chr(int(m[1], 8)), field) == target:
                return True
        return False
    try:
        os.lstat(mountpoint)
    except OSError as e:
        return e.errno in DEAD_MOUNT_ERRNOS and Path(mountpoint).name in os.listdir(Path(mountpoint).parent)
    return os.path.ismount(mountpoint)


def unmount(mountpoint: Path, grace: float = 3) -> bool:
    """Unmount a mountpoint, forcing it if the normal unmount fails. Never raises.

    Args:
        mountpoint: Local mountpoint to detach.
        grace: Seconds each unmount command may take. Defaults to 3.

    Returns:
        True if nothing is mounted at `mountpoint` afterwards.
    """
    if not is_mounted(mountpoint):
        return True
    for argv in platform.unmount_cmds(str(mountpoint)):
        try:
            subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=grace)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not is_mounted(mountpoint):
            return True
    return False
