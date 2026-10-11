"""Integration tests of the host lifecycle: connect, exec, mounts, failure isolation, reconnect and teardown."""

from __future__ import annotations

import re
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from conftest import ROOT, Mcp, base_env, descendants, is_running, pids_matching, release_leftovers, run_hook, runtime_dir_of, start_session_hook, wait_for

from remote_ssh import mount, state

TEARDOWN_LIMIT_S = 10.0
RECONNECT_LIMIT_S = 45.0


def assert_released(rt: Path, alias: str) -> None:
    """Assert that no mount, ssh process or control socket of `alias` remains."""
    host_dir = rt / alias
    assert not mount.is_mounted(host_dir / 'fs')
    assert not mount.is_mounted(host_dir / 'mem')
    assert not (host_dir / 'c').exists()
    #--- sshfs exits a moment after its lazy unmount, so give it a few seconds ---
    assert wait_for(lambda: pids_matching(str(host_dir / 'c')) == [], timeout=5), pids_matching(str(host_dir / 'c'))


def master_pids(rt: Path, alias: str) -> list[int]:
    """Return the pids of the ControlMaster of `alias`."""
    return pids_matching(f'-M -N -S {rt / alias / "c"}')


#---------------------------------------------------------------------
# Connect, exec, mount and disconnect
#---------------------------------------------------------------------

def test_connect_exec_mount_and_disconnect(session: Any, sshd: dict, remote_dir: Path) -> None:
    (remote_dir / 'hello.txt').write_text('hello\n')
    sess = session('a')

    connect_text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, connect_text
    assert connect_text.startswith('connect h1 · ') and f'\npath: {remote_dir}\n' in connect_text
    assert '\nbash: runs here (remote_mode("h1") runs it on h1)' in connect_text
    assert len(master_pids(sess.rt, 'h1')) == 1

    #--- remote_exec runs in the project path and passes the exit code through ---
    text, is_error = sess.mcp.call('remote_exec', alias='h1', command='pwd; echo out; echo err >&2; exit 3')
    assert not is_error
    assert text.startswith('exec h1 · exit 3')
    assert f'{remote_dir}\nout' in text and 'err' in text
    text, is_error = sess.mcp.call('remote_exec', alias='h1', command='true')
    assert not is_error and text.startswith('exec h1 · exit 0')

    #--- the file mount shows remote files and writes through to them ---
    fs_dir = sess.rt / 'h1' / 'fs'
    assert os.path.ismount(fs_dir), connect_text
    assert (fs_dir / 'hello.txt').read_text() == 'hello\n'
    (fs_dir / 'hello.txt').write_text('edited\n')
    (fs_dir / 'new.txt').write_text('created\n')
    assert (remote_dir / 'hello.txt').read_text() == 'edited\n'
    assert (remote_dir / 'new.txt').read_text() == 'created\n'

    text, is_error = sess.mcp.call('remote_disconnect', alias='h1')
    assert not is_error and text.startswith('disconnect h1'), text
    assert_released(sess.rt, 'h1')
    assert 'h1' not in state.read_json(sess.rt / 'state.json')['hosts']
    assert (remote_dir / 'hello.txt').read_text() == 'edited\n'


def test_disconnect_releases_mount_whose_remote_directory_vanished(session: Any, sshd: dict, tmp_path: Path) -> None:
    gone = (tmp_path / 'remote-gone').resolve()
    gone.mkdir()
    sess = session('a')
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(gone))
    assert not is_error, text
    gone.rmdir()

    text, is_error = sess.mcp.call('remote_disconnect', alias='h1')
    assert not is_error, text
    assert_released(sess.rt, 'h1')


#---------------------------------------------------------------------
# Two hosts and failure isolation
#---------------------------------------------------------------------

def test_failing_host_does_not_affect_the_other_host(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    other_dir = (tmp_path / 'remote-other').resolve()
    other_dir.mkdir()
    sess = session('a')

    #--- three connects in flight at once: two good, one to an unresolvable host ---
    ids = {
        'h1': sess.mcp.send('tools/call', {'name': 'remote_connect', 'arguments': {'alias': 'h1', 'host': sshd['host'], 'path': str(remote_dir)}}),
        'h2': sess.mcp.send('tools/call', {'name': 'remote_connect', 'arguments': {'alias': 'h2', 'host': 'no-such-host.invalid', 'path': '/'}}),
        'h3': sess.mcp.send('tools/call', {'name': 'remote_connect', 'arguments': {'alias': 'h3', 'host': sshd['host'], 'path': str(other_dir)}}),
    }
    results = {alias: Mcp.result(sess.mcp.wait(msg_id)) for alias, msg_id in ids.items()}
    assert not results['h1'][1], results['h1'][0]
    assert not results['h3'][1], results['h3'][0]
    assert results['h2'][1] and results['h2'][0].startswith('connect h2 · error'), results['h2'][0]
    assert_released(sess.rt, 'h2')

    text, _ = sess.mcp.call('remote_status')
    assert text.startswith('status · 3 hosts'), text
    assert all(re.search(rf'^{alias}\s+{word}\s', text, re.M) for alias, word in (('h1', 'connected'), ('h3', 'connected'), ('h2', 'failed'))), text

    for alias, path in (('h1', remote_dir), ('h3', other_dir)):
        text, is_error = sess.mcp.call('remote_exec', alias=alias, command='pwd')
        assert not is_error and f'{path}\n' in text, text
        assert os.path.ismount(sess.rt / alias / 'fs')

    #--- dropping one host leaves the other untouched ---
    text, is_error = sess.mcp.call('remote_disconnect', alias='h1')
    assert not is_error, text
    assert_released(sess.rt, 'h1')
    text, is_error = sess.mcp.call('remote_exec', alias='h3', command='echo still-up')
    assert not is_error and 'still-up' in text
    (sess.rt / 'h3' / 'fs' / 'x.txt').write_text('x')
    assert (other_dir / 'x.txt').read_text() == 'x'


def test_missing_remote_directory_fails_with_clear_error(session: Any, sshd: dict, tmp_path: Path) -> None:
    sess = session('a')
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(tmp_path / 'absent'))
    assert is_error
    assert 'does not exist' in text, text
    assert_released(sess.rt, 'h1')


#---------------------------------------------------------------------
# Reconnect after a killed master
#---------------------------------------------------------------------

def test_killed_master_reconnects_and_remounts(session: Any, sshd: dict, remote_dir: Path) -> None:
    (remote_dir / 'before.txt').write_text('before\n')
    sess = session('a')
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    [old_pid] = master_pids(sess.rt, 'h1')

    os.kill(old_pid, signal.SIGKILL)
    killed_at = time.monotonic()

    def reconnected() -> bool:
        pids = master_pids(sess.rt, 'h1')
        status, _ = sess.mcp.call('remote_status')
        return bool(pids) and old_pid not in pids and re.search(r'^h1\s+connected\s', status, re.M) is not None

    assert wait_for(reconnected, RECONNECT_LIMIT_S, interval=0.5), sess.mcp.call('remote_status')[0]
    print(f'\nmaster back after {time.monotonic() - killed_at:.1f} s')

    fs_dir = sess.rt / 'h1' / 'fs'
    assert wait_for(lambda: os.path.ismount(fs_dir), RECONNECT_LIMIT_S - (time.monotonic() - killed_at))

    def mount_usable() -> bool:
        try:
            return (fs_dir / 'before.txt').read_text() == 'before\n'
        except OSError:
            return False

    assert wait_for(mount_usable, RECONNECT_LIMIT_S - (time.monotonic() - killed_at))
    (fs_dir / 'after.txt').write_text('after\n')
    assert (remote_dir / 'after.txt').read_text() == 'after\n'
    text, is_error = sess.mcp.call('remote_exec', alias='h1', command='cat after.txt')
    assert not is_error and 'after' in text


def test_remote_exec_timeout_leaves_no_remote_process(session: Any, sshd: dict, remote_dir: Path) -> None:
    sess = session('a')
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    marker = f'sleep 7{os.getpid() % 1000:03d}'
    started = time.monotonic()
    text, is_error = sess.mcp.call('remote_exec', alias='h1', command=f'{marker}; echo done', timeout_s=2)
    assert is_error and text.startswith('exec h1 · exit 124'), text
    assert time.monotonic() - started < 15
    assert wait_for(lambda: not pids_matching(marker), 5), f'{marker} still runs on the remote'


#---------------------------------------------------------------------
# Teardown
#---------------------------------------------------------------------

def test_session_end_with_frozen_host_releases_everything(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    """The remote stops answering: the sshd processes of this session's connection are SIGSTOPped."""
    sess = session('a')
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    remote_reg = tmp_path / 'remote-msg' / 'sessions' / f'{sess.sid}.json'
    assert remote_reg.exists()
    frozen = descendants(sshd['pid'])
    assert frozen, 'no sshd session process found'
    for pid in frozen:
        os.kill(pid, signal.SIGSTOP)
    try:
        started = time.monotonic()
        run_hook(sess.env, 'SessionEnd', {'session_id': sess.sid, 'reason': 'other'})
        hook_s = time.monotonic() - started
        assert hook_s < 1.5, 'SessionEnd must return within the hook budget'
        assert wait_for(lambda: not sess.rt.joinpath('shutdown').exists(), TEARDOWN_LIMIT_S)
        elapsed = time.monotonic() - started
        print(f'\nSessionEnd with a frozen host took {elapsed:.2f} s')
        assert elapsed < TEARDOWN_LIMIT_S
        assert_released(sess.rt, 'h1')
        assert state.read_json(sess.rt / 'state.json')['hosts'] == {}
        assert sess.mcp.proc.poll() is None, 'SessionEnd must not end the server process'
    finally:
        for pid in frozen:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
    text, _ = sess.mcp.call('remote_status')
    assert text.startswith('status · 0 hosts'), text


def test_sigkill_of_parent_claude_tears_everything_down(sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    """The stand-in Claude is the server's parent shell, as Claude is for a real server."""
    rt_root = tmp_path / 'rt'
    env = base_env(sshd, tmp_path, 'a')
    cwd = tmp_path / 'a'
    cwd.mkdir()
    #--- `; exit` keeps sh alive as the parent instead of exec-ing the server ---
    wrapper = ['sh', '-c', 'REMOTE_SSH_CLAUDE_PID=$$ "$0" "$@"; exit $?', sys.executable, str(ROOT / 'scripts' / 'server.py')]
    mcp = Mcp(env, argv=wrapper)
    try:
        claude_pid = mcp.proc.pid
        hook_env = {**env, 'REMOTE_SSH_CLAUDE_PID': str(claude_pid)}
        sid = start_session_hook(hook_env, 'a', cwd)
        rt = runtime_dir_of(rt_root, claude_pid)
        local_reg = tmp_path / 'msg-a' / 'sessions' / f'{sid}.json'
        remote_reg = tmp_path / 'remote-msg' / 'sessions' / f'{sid}.json'

        assert wait_for(local_reg.exists, 15)
        text, is_error = mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
        assert not is_error, text
        server_pid = state.read_json(rt / 'owner.json')['server_pid']
        assert os.path.ismount(rt / 'h1' / 'fs')
        assert master_pids(rt, 'h1') and remote_reg.exists()
        assert is_running(server_pid) and server_pid != claude_pid

        mcp.proc.send_signal(signal.SIGKILL)
        killed_at = time.monotonic()
        mcp.proc.wait()

        def torn_down() -> bool:
            return (not is_running(server_pid) and not os.path.ismount(rt / 'h1' / 'fs') and not master_pids(rt, 'h1')
                    and not (rt / 'h1' / 'c').exists() and not local_reg.exists() and not remote_reg.exists())

        done = wait_for(torn_down, 20, interval=0.05)
        elapsed = time.monotonic() - killed_at
        print(f'\nteardown after SIGKILL of the Claude stand-in took {elapsed:.2f} s')
        assert done, 'teardown incomplete after 20 s'
        assert elapsed <= TEARDOWN_LIMIT_S
        assert_released(rt, 'h1')
    finally:
        #--- EOF on stdin stops an orphaned server if the test failed before it exited ---
        if mcp.proc.poll() is None:
            mcp.proc.kill()
            mcp.proc.wait()
        mcp.proc.stdin.close()
        release_leftovers(rt_root)
