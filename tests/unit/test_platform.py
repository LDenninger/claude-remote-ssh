"""Unit tests for remote_ssh.platform."""

from __future__ import annotations

import pytest

from remote_ssh import platform


def test_detect_reports_all_tool_keys() -> None:
    detected = platform.detect()
    assert set(detected) == {'ssh', 'sshfs', 'fuse', 'python'}
    assert detected['python'] is True


def test_detect_linux_fuse_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, 'IS_MAC', False)
    monkeypatch.setattr(platform.shutil, 'which', lambda name: '/bin/fusermount' if name == 'fusermount' else None)
    assert platform.detect() == {'ssh': False, 'sshfs': False, 'fuse': True, 'python': False}


def test_missing_lists_unavailable_tools() -> None:
    assert platform.missing({'ssh': True, 'sshfs': False, 'fuse': False, 'python': True}) == ['sshfs', 'fuse']
    assert platform.missing({'ssh': True}) == []


def test_missing_message_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, 'IS_MAC', False)
    assert platform.missing_message([]) == ''
    lines = platform.missing_message(['sshfs', 'fuse']).splitlines()
    assert len(lines) == 2
    assert 'apt install sshfs' in lines[0]
    assert 'apt install fuse3' in lines[1]


def test_missing_message_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, 'IS_MAC', True)
    message = platform.missing_message(['sshfs', 'fuse'])
    assert 'brew install' in message
    assert 'https://macfuse.github.io' in message


def test_unmount_cmds_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, 'IS_MAC', False)
    monkeypatch.setattr(platform.shutil, 'which', lambda name: '/usr/bin/fusermount3' if name == 'fusermount3' else None)
    assert platform.unmount_cmds('/m') == (['fusermount3', '-u', '/m'], ['fusermount3', '-uz', '/m'])
    monkeypatch.setattr(platform.shutil, 'which', lambda name: None)
    assert platform.unmount_cmds('/m') == (['fusermount', '-u', '/m'], ['fusermount', '-uz', '/m'])


def test_unmount_cmds_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, 'IS_MAC', True)
    assert platform.unmount_cmds('/m') == (['umount', '/m'], ['diskutil', 'unmount', 'force', '/m'])
