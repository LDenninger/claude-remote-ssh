"""Integration test of the message round trip between a local and a "remote" session.

Session A connects to the sshd host. Session B plays a session running on that host: its
local message base is the remote base A reaches over ssh (`REMOTE_SSH_REMOTE_BASE`), so B's
own server registers it there, B's `remote_send` without alias routes through the outbox
to A, and A's lifecycle loop pulls that outbox, all on the production code paths.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from conftest import pids_matching, run_hook, wait_for

POLL_INTERVAL_S = 1.0
RECONNECT_LIMIT_S = 45.0


def inbox_text(sess: Any) -> str:
    """Return the `remote_inbox` text of a session, failing on a tool error."""
    text, is_error = sess.mcp.call('remote_inbox')
    assert not is_error, text
    return text


def assert_arrives(sess: Any, body: str, limit_s: float) -> float:
    """Assert that `body` reaches the inbox of `sess` within `limit_s` and return the delay."""
    start = time.monotonic()
    seen = wait_for(lambda: body in inbox_text(sess), limit_s + 2, interval=0.1)
    delay = time.monotonic() - start
    assert seen, f'{body!r} not delivered'
    assert delay <= limit_s, f'{body!r} took {delay:.2f} s, limit {limit_s} s'
    return delay


def test_message_round_trip_including_after_reconnect(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    remote_base = tmp_path / 'remote-msg'
    sess_b = session('b', REMOTE_SSH_MSG_BASE=str(remote_base))
    assert wait_for((remote_base / 'sessions' / f'{sess_b.sid}.json').exists, 15)
    sess_a = session('a')
    assert wait_for((tmp_path / 'msg-a' / 'sessions' / f'{sess_a.sid}.json').exists, 15)

    text, is_error = sess_a.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    assert f'({sess_b.sid[:8]})' in text, text
    assert (remote_base / 'sessions' / f'{sess_a.sid}.json').exists()

    #--- A -> B lands directly in B's inbox on the remote ---
    text, is_error = sess_a.mcp.call('remote_send', alias='h1', session=sess_b.sid, body='ping from A')
    assert not is_error and 'inbox on h1' in text, text
    assert 'ping from A' in inbox_text(sess_b)

    #--- B -> A goes to B's outbox and A's loop pulls it within two poll intervals ---
    text, is_error = sess_b.mcp.call('remote_send', session=sess_a.sid, body='pong from B')
    assert not is_error and 'via outbox' in text, text
    delay = assert_arrives(sess_a, 'pong from B', 2 * POLL_INTERVAL_S)
    print(f'\noutbox pull took {delay:.2f} s')
    assert 'relayed by host h1' in inbox_text(sess_a)

    #--- a reply written while A's master is down is pulled once it is back ---
    [master_pid] = pids_matching(f'-M -N -S {sess_a.rt / "h1" / "c"}')
    os.kill(master_pid, signal.SIGKILL)
    text, is_error = sess_b.mcp.call('remote_send', session=sess_a.sid, body='sent during outage')
    assert not is_error and 'via outbox' in text, text

    def reconnected() -> bool:
        pids = pids_matching(f'-M -N -S {sess_a.rt / "h1" / "c"}')
        return bool(pids) and master_pid not in pids and 'h1: connected' in sess_a.mcp.call('remote_status')[0]

    assert wait_for(reconnected, RECONNECT_LIMIT_S, interval=0.2)
    delay = assert_arrives(sess_a, 'sent during outage', 2 * POLL_INTERVAL_S)
    print(f'pull after reconnect took {delay:.2f} s')
    #--- pull_outbox removes the pulled files in a second round trip, after local delivery ---
    assert wait_for(lambda: not any((remote_base / 'outbox' / sess_a.sid).glob('*.json')), POLL_INTERVAL_S)


def test_clear_moves_registration_to_new_session_id(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    sess = session('a')
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    local_sessions, remote_sessions = tmp_path / 'msg-a' / 'sessions', tmp_path / 'remote-msg' / 'sessions'
    assert (local_sessions / f'{sess.sid}.json').exists() and (remote_sessions / f'{sess.sid}.json').exists()

    #--- /clear: SessionEnd(reason=clear) keeps the server, SessionStart writes a new id ---
    run_hook(sess.env, 'SessionEnd', {'session_id': sess.sid, 'reason': 'clear'})
    new_sid = 'c1ea0000-0000-0000-0000-000000000000'
    run_hook(sess.env, 'SessionStart', {'session_id': new_sid, 'cwd': str(sess.cwd), 'source': 'clear'})

    def moved() -> bool:
        return ({path.stem for path in local_sessions.glob('*.json')} == {new_sid}
                and {path.stem for path in remote_sessions.glob('*.json')} == {new_sid})

    assert wait_for(moved, 2 * POLL_INTERVAL_S + 1), (list(local_sessions.iterdir()), list(remote_sessions.iterdir()))
    assert sess.mcp.proc.poll() is None
    text, _ = sess.mcp.call('remote_status')
    assert 'h1: connected' in text and f'session {new_sid}' in text


def test_send_to_relayed_session_goes_through_remote_outbox(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    remote_base = tmp_path / 'remote-msg'
    sess_c = session('c', REMOTE_SSH_MSG_BASE=str(remote_base))
    sess_a, sess_b = session('a'), session('b')
    for sess in (sess_a, sess_b):
        text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
        assert not is_error, text
    assert {path.stem for path in (remote_base / 'sessions').glob('*.json')} == {sess_a.sid, sess_b.sid, sess_c.sid}
    assert (tmp_path / 'msg-a').resolve() != (tmp_path / 'msg-b').resolve()

    #--- A -> B: B is registered on the host with relay_via, so the message waits in B's outbox ---
    text, is_error = sess_a.mcp.call('remote_send', alias='h1', session=sess_b.sid, body='hello B via relay')
    assert not is_error and 'outbox on h1' in text, text
    assert wait_for(lambda: 'hello B via relay' in inbox_text(sess_b), 2 * POLL_INTERVAL_S + 3)
    assert 'relayed by host h1' in inbox_text(sess_b)
    assert not any((remote_base / 'outbox' / sess_b.sid).glob('*.json'))
    assert not (remote_base / 'inbox' / sess_b.sid).exists()

    #--- A -> C: C runs on the host itself, so the message lands in its inbox ---
    text, is_error = sess_a.mcp.call('remote_send', alias='h1', session=sess_c.sid, body='hello C direct')
    assert not is_error and 'inbox on h1' in text, text
    assert 'hello C direct' in inbox_text(sess_c)
