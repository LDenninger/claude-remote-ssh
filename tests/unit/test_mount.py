"""Unit tests for remote_ssh.mount: sshfs argv and the unmount fallback order."""

from __future__ import annotations

import errno
import importlib
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import remote_ssh
from remote_ssh.conn import Master
from remote_ssh.mount import MountError, sshfs_argv, ssh_command_option


@pytest.fixture
def mount_mod(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Import remote_ssh.mount, standing in a temporary platform module if none exists yet."""
    try:
        importlib.import_module('remote_ssh.platform')
    except ImportError:
        stub = types.ModuleType('remote_ssh.platform')
        stub.IS_MAC = False
        stub.unmount_cmds = lambda mountpoint: (['fusermount3', '-u', mountpoint], ['fusermount3', '-uz', mountpoint])
        monkeypatch.setitem(sys.modules, 'remote_ssh.platform', stub)
        monkeypatch.setattr(remote_ssh, 'platform', stub, raising=False)
        monkeypatch.delitem(sys.modules, 'remote_ssh.mount', raising=False)
    return importlib.import_module('remote_ssh.mount')


def test_sshfs_argv_linux(mount_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mount_mod.platform, 'IS_MAC', False)
    master = Master('gpu', 'user@gpu', Path('/run/c'))
    assert mount_mod.sshfs_argv(master, '/srv/p', Path('/run/fs')) == [
        'sshfs', 'user@gpu:/srv/p', '/run/fs', '-o', 'ssh_command=ssh -S /run/c -o BatchMode=yes -o ControlMaster=no', '-o', 'reconnect,ServerAliveInterval=15',
    ]


def test_sshfs_argv_mac_extra(mount_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mount_mod.platform, 'IS_MAC', True)
    master = Master('gpu', 'gpu', Path('/run/c'), ['-p', '2222', '-i', '/k'])
    argv = mount_mod.sshfs_argv(master, '/srv/p', Path('/run/fs'), extra_opts='cache=no')
    assert argv[3:] == [
        '-o', 'ssh_command=ssh -S /run/c -o BatchMode=yes -o ControlMaster=no -p 2222 -i /k', '-o', 'reconnect,ServerAliveInterval=15',
        '-o', 'volname=gpu,noappledouble', '-o', 'cache=no',
    ]


def test_mount_failure_raises_with_sshfs_stderr(mount_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, b'', b'fuse: device not found'))
    with pytest.raises(mount_mod.MountError, match='fuse: device not found'):
        mount_mod.mount(Master('a', 'h', Path('/run/c')), '/srv', tmp_path / 'fs')


def test_unmount_not_mounted_is_noop(mount_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('no command expected'))
    assert mount_mod.unmount(tmp_path) is True


def test_unmount_falls_back_to_forced(mount_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(mount_mod.platform, 'unmount_cmds', lambda mp: (['normal', mp], ['forced', mp]))
    monkeypatch.setattr(mount_mod, 'is_mounted', lambda mp: not calls or calls[-1][0] != 'forced')

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'normal':
            raise subprocess.TimeoutExpired(argv, 3)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert mount_mod.unmount(Path('/mnt/x')) is True
    assert calls == [['normal', '/mnt/x'], ['forced', '/mnt/x']]


def test_unmount_reports_failure(mount_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mount_mod.platform, 'unmount_cmds', lambda mp: (['normal', mp], ['forced', mp]))
    monkeypatch.setattr(mount_mod, 'is_mounted', lambda mp: True)
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: (_ for _ in ()).throw(OSError('missing')))
    assert mount_mod.unmount(Path('/mnt/x')) is False


def test_is_mounted_reads_kernel_mount_table(tmp_path, monkeypatch):
    from remote_ssh import mount as mount_mod
    table = tmp_path / 'mountinfo'
    table.write_text(
        '36 35 98:0 / /mnt/a\\040b rw,noatime master:1 - fuse.sshfs h:/x rw\n'
        '37 35 98:0 / /plain rw - ext4 /dev/sda1 rw\n'
    )
    monkeypatch.setattr(mount_mod, 'MOUNTINFO', table)
    assert mount_mod.is_mounted(Path('/mnt/a b'))
    assert mount_mod.is_mounted(Path('/plain'))
    assert not mount_mod.is_mounted(Path('/mnt/a'))


def test_is_mounted_resolves_symlinked_parent(tmp_path, monkeypatch):
    """The kernel lists resolved paths, so a runtime base behind a symlink must still match."""
    from remote_ssh import mount as mount_mod
    real_dir = tmp_path / 'real'
    (real_dir / 'fs').mkdir(parents=True)
    (tmp_path / 'link').symlink_to(real_dir)
    table = tmp_path / 'mountinfo'
    table.write_text(f'36 35 98:0 / {real_dir}/fs rw - fuse.sshfs h:/x rw\n')
    monkeypatch.setattr(mount_mod, 'MOUNTINFO', table)
    assert mount_mod.is_mounted(tmp_path / 'link' / 'fs')


def test_is_mounted_counts_dead_mount_without_mount_table(tmp_path, monkeypatch):
    from remote_ssh import mount as mount_mod
    monkeypatch.setattr(mount_mod, 'MOUNTINFO', tmp_path / 'absent')
    dead = tmp_path / 'fs'
    dead.mkdir()
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == dead:
            raise OSError(errno.ENOTCONN, 'Transport endpoint is not connected')
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(mount_mod.os, 'lstat', lstat)
    assert mount_mod.is_mounted(dead)
    assert not mount_mod.is_mounted(tmp_path / 'missing')


#---------------------------------------------------------------------
# sshfs ssh_command option
#---------------------------------------------------------------------

def test_ssh_command_escapes_spaces_and_backslashes_without_quotes() -> None:
    master = Master('a', 'h', Path('/run/my dir/c'), ['-i', '/k/a\\b c'])
    assert ssh_command_option(master) == 'ssh -S /run/my\\ dir/c -o BatchMode=yes -o ControlMaster=no -i /k/a\\\\b\\ c'
    assert "'" not in ssh_command_option(master)


def test_ssh_command_rejects_comma() -> None:
    with pytest.raises(MountError, match='comma'):
        sshfs_argv(Master('a', 'h', Path('/run/c,d')), '/p', Path('/m'))
