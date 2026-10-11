"""Platform differences between Linux and macOS: tool detection, install hints, unmounting.

`detect` reports which external tools the plugin needs are present, `missing_message` turns the
absent ones into install instructions for this OS, and `unmount_cmds` returns the normal and the
forced unmount command for a FUSE mountpoint.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

IS_MAC = sys.platform == 'darwin'

_INSTALL_LINUX = {
    'ssh': 'sudo apt install openssh-client',
    'sshfs': 'sudo apt install sshfs',
    'fuse': 'sudo apt install fuse3',
    'python': 'sudo apt install python3',
}
_INSTALL_MAC = {
    'ssh': 'brew install openssh',
    'sshfs': 'brew install gromgit/fuse/sshfs-mac',
    'fuse': 'install macFUSE from https://macfuse.github.io',
    'python': 'brew install python',
}


def detect() -> dict[str, bool]:
    """Return which of the tools `ssh`, `sshfs`, `fuse` and `python` are available."""
    if IS_MAC:
        fuse = Path('/Library/Filesystems/macfuse.fs').exists()
    else:
        fuse = shutil.which('fusermount3') is not None or shutil.which('fusermount') is not None
    return {
        'ssh': shutil.which('ssh') is not None,
        'sshfs': shutil.which('sshfs') is not None,
        'fuse': fuse,
        'python': shutil.which('python3') is not None,
    }


def missing(d: dict[str, bool]) -> list[str]:
    """Return the names of the tools `detect` reported absent, in detection order."""
    return [name for name, present in d.items() if not present]


def missing_message(missing: list[str]) -> str:
    """Return install instructions for absent tools, one line each, or "" if none is absent."""
    install = _INSTALL_MAC if IS_MAC else _INSTALL_LINUX
    return '\n'.join(f'remote-ssh: {name} is missing, {install.get(name, "install it")}' for name in missing)


def unmount_cmds(mountpoint: str) -> tuple[list[str], list[str]]:
    """Return the normal and the forced unmount command for a FUSE mountpoint.

    Returns:
        A tuple of argv lists:
            the normal unmount, tried first,
            the forced or lazy unmount, used when the normal one leaves the mount in place.
    """
    if IS_MAC:
        return ['umount', mountpoint], ['diskutil', 'unmount', 'force', mountpoint]
    fusermount = 'fusermount3' if shutil.which('fusermount3') else 'fusermount'
    return [fusermount, '-u', mountpoint], [fusermount, '-uz', mountpoint]
