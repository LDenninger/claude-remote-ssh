"""Integration test: a message reaches a session that is only reachable through the remote relay.

Machines A and B are two "local" machines with message bases of their own, both connected to the
same remote host. Session C runs on that host, so its message base is the remote base. A sees B
only as a remote registration carrying `relay_via`, so A's `remote_send` must go to B's outbox
on the host, from where B's lifecycle loop pulls it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import wait_for

POLL_INTERVAL_S = 1.0


def inbox_text(sess: Any) -> str:
    """Return the `remote_inbox` text of a session, failing on a tool error."""
    text, is_error = sess.mcp.call('remote_inbox')
    assert not is_error, text
    return text


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
