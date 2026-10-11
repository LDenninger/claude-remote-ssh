"""Integration test of the message round trip between a local and a "remote" session.

Session A connects to the sshd host. Session B plays a session running on that host: its
local message base is the remote base A reaches over ssh (`REMOTE_SSH_REMOTE_BASE`), so B's
own server registers it there, B's `remote_send` without alias routes through the outbox
to A, and A's lifecycle loop pulls that outbox, all on the production code paths.
"""

from __future__ import annotations

import json
import re
import os
import signal
import socket
import time
from pathlib import Path
from typing import Any

import pytest

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
    assert text.startswith('connect h1 · 1 session · ') and '\nsessions: ' in text, text
    assert (remote_base / 'sessions' / f'{sess_a.sid}.json').exists()

    #--- A -> B lands directly in B's inbox on the remote ---
    text, is_error = sess_a.mcp.call('remote_send', alias='h1', session=sess_b.sid, body='ping from A')
    assert not is_error and text.startswith('send h1 · ') and ' · delivered\n' in text, text
    assert 'ping from A' in inbox_text(sess_b)

    #--- B -> A goes to B's outbox and A's loop pulls it within two poll intervals ---
    text, is_error = sess_b.mcp.call('remote_send', session=sess_a.sid, body='pong from B')
    assert not is_error and ' · queued for pickup\n' in text, text
    delay = assert_arrives(sess_a, 'pong from B', 2 * POLL_INTERVAL_S)
    print(f'\noutbox pull took {delay:.2f} s')
    assert '\nh1/' in inbox_text(sess_a)

    #--- a reply written while A's master is down is pulled once it is back ---
    [master_pid] = pids_matching(f'-M -N -S {sess_a.rt / "h1" / "c"}')
    os.kill(master_pid, signal.SIGKILL)
    text, is_error = sess_b.mcp.call('remote_send', session=sess_a.sid, body='sent during outage')
    assert not is_error and ' · queued for pickup\n' in text, text

    def reconnected() -> bool:
        pids = pids_matching(f'-M -N -S {sess_a.rt / "h1" / "c"}')
        return bool(pids) and master_pid not in pids and re.search(r'^h1\s+connected\s', sess_a.mcp.call('remote_status')[0], re.M) is not None

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
    assert re.search(r'^h1\s+connected\s', text, re.M) and f'session: {new_sid}' in text


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
    assert not is_error and ' · queued for pickup\n' in text, text
    assert wait_for(lambda: 'hello B via relay' in inbox_text(sess_b), 2 * POLL_INTERVAL_S + 3)
    assert '\nh1/' in inbox_text(sess_b)
    assert not any((remote_base / 'outbox' / sess_b.sid).glob('*.json'))
    assert not (remote_base / 'inbox' / sess_b.sid).exists()

    #--- A -> C: C runs on the host itself, so the message lands in its inbox ---
    text, is_error = sess_a.mcp.call('remote_send', alias='h1', session=sess_c.sid, body='hello C direct')
    assert not is_error and ' · delivered\n' in text, text
    assert 'hello C direct' in inbox_text(sess_c)


def test_clear_keeps_the_session_name_everywhere(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    sess = session('a')
    text, is_error = sess.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    local_entry, remote_entry = tmp_path / 'msg-a' / 'sessions', tmp_path / 'remote-msg' / 'sessions'
    old_name = json.loads((local_entry / f'{sess.sid}.json').read_text())['name']

    run_hook(sess.env, 'SessionEnd', {'session_id': sess.sid, 'reason': 'clear'})
    new_sid = 'c1ea0000-0000-0000-0000-000000000000'
    run_hook(sess.env, 'SessionStart', {'session_id': new_sid, 'cwd': str(sess.cwd), 'source': 'clear'})
    assert wait_for(lambda: (remote_entry / f'{new_sid}.json').exists() and (local_entry / f'{new_sid}.json').exists(), 2 * POLL_INTERVAL_S + 1)
    for path in (local_entry / f'{new_sid}.json', remote_entry / f'{new_sid}.json'):
        entry = json.loads(path.read_text())
        assert (entry['name'], entry['previous_ids']) == (old_name, [sess.sid])
    assert old_name in sess.mcp.call('remote_sessions', alias='h1')[0]


def test_resumed_process_takes_over_the_name_and_receives_old_mail(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    proj = tmp_path / 'proj'
    first = session('a', cwd=proj)
    sess_c = session('c', REMOTE_SSH_MSG_BASE=str(tmp_path / 'remote-msg'))
    old_name = json.loads((tmp_path / 'msg-a' / 'sessions' / f'{first.sid}.json').read_text())['name']
    first.mcp.close()
    assert wait_for(lambda: not (tmp_path / 'msg-a' / 'sessions' / f'{first.sid}.json').exists(), 10)

    #--- a new Claude process in the same directory, with a new session id, on the same message base ---
    resumed = session('r', cwd=proj, REMOTE_SSH_MSG_BASE=str(tmp_path / 'msg-a'))
    entry_path = tmp_path / 'msg-a' / 'sessions' / f'{resumed.sid}.json'
    assert wait_for(entry_path.exists, 15)
    entry = json.loads(entry_path.read_text())
    assert (entry['name'], entry['previous_ids']) == (old_name, [first.sid])

    #--- C, on the host, addresses the resumed session by its old id; its remote registration carries previous_ids ---
    text, is_error = resumed.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    text, is_error = sess_c.mcp.call('remote_send', session=first.sid, body='mail for the old id')
    assert not is_error and ' · queued for pickup\n' in text, text
    assert wait_for(lambda: 'mail for the old id' in inbox_text(resumed), 2 * POLL_INTERVAL_S + 3)


@pytest.mark.parametrize('chars', [1800, 7900])
def test_long_messages_arrive_whole_on_every_path(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path, chars: int) -> None:
    sess_a, sess_b = session('a'), session('b', REMOTE_SSH_MSG_BASE=str(tmp_path / 'remote-msg'))
    text, is_error = sess_a.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    line = 'He said "stop", it\'s `$HOME` \\n done; rm -rf * # \'\'\' """\n'
    body = (line * (chars // len(line) + 1))[:chars]
    stored = tmp_path / 'remote-msg' / 'inbox' / sess_b.sid

    #--- A -> B over ssh stdin into B's inbox ---
    text, is_error = sess_a.mcp.call('remote_send', alias='h1', session=sess_b.sid, body=body)
    assert not is_error, text
    [path] = stored.glob('*.json')
    assert json.loads(path.read_text())['body'] == body
    #--- the hook path injects it whole or as a one-line notice that remote_inbox then resolves ---
    context = run_hook(sess_b.env, 'UserPromptSubmit', {'session_id': sess_b.sid})['hookSpecificOutput']['additionalContext']
    assert len(context) <= 8000 and context.count('\n') < 200
    if chars == 1800:
        assert '\n'.join(context.split('\n')[3:]) == '\n'.join(f'> {part}'.rstrip() for part in body.splitlines())
    else:
        assert context == f'message {socket.gethostname()} · a-a-00 · {chars} chars, call remote_inbox to read it'
        shown = inbox_text(sess_b).split('--- delivered ---\n', 1)[1]
        assert '\n'.join(row[2:] for row in shown.split('\n')[1:]) == '\n'.join(body.splitlines())

    #--- B -> A through the outbox, pulled by A, then A's monitor line view ---
    text, is_error = sess_b.mcp.call('remote_send', session=sess_a.sid, body=body)
    assert not is_error, text
    assert wait_for(lambda: list((tmp_path / 'msg-a' / 'inbox' / sess_a.sid).glob('*.json')), 2 * POLL_INTERVAL_S + 2)
    [pulled] = (tmp_path / 'msg-a' / 'inbox' / sess_a.sid).glob('*.json')
    assert json.loads(pulled.read_text())['body'] == body


def test_files_are_attached_in_both_directions(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    sess_a, sess_b = session('a'), session('b', REMOTE_SSH_MSG_BASE=str(tmp_path / 'remote-msg'))
    text, is_error = sess_a.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    payload = bytes(range(256)) * 40
    (sess_a.cwd / 'blob.bin').write_bytes(payload)
    (sess_b.cwd / 'notes.txt').write_text('from B\n')

    text, is_error = sess_a.mcp.call('remote_send', alias='h1', session=sess_b.sid, body='see blob', files=['blob.bin'])
    assert not is_error and 'attachments: 1 file, ' in text, text
    [msg_path] = (tmp_path / 'remote-msg' / 'inbox' / sess_b.sid).glob('*.json')
    [remote_path] = json.loads(msg_path.read_text())['attachments']
    assert Path(remote_path).read_bytes() == payload
    assert f'attachments: {remote_path}\n' in run_hook(sess_b.env, 'UserPromptSubmit', {'session_id': sess_b.sid})['hookSpecificOutput']['additionalContext']

    text, is_error = sess_b.mcp.call('remote_send', session=sess_a.sid, body='my notes', files=['notes.txt'])
    assert not is_error, text
    assert wait_for(lambda: list((tmp_path / 'msg-a' / 'inbox' / sess_a.sid).glob('*.json')), 2 * POLL_INTERVAL_S + 2)
    [pulled_path] = (tmp_path / 'msg-a' / 'inbox' / sess_a.sid).glob('*.json')
    [local_path] = json.loads(pulled_path.read_text())['attachments']
    assert Path(local_path).read_text() == 'from B\n' and str(tmp_path / 'msg-a') in local_path


def test_receipts_follow_a_remote_message(session: Any, sshd: dict, remote_dir: Path, tmp_path: Path) -> None:
    sess_a, sess_b = session('a'), session('b', REMOTE_SSH_MSG_BASE=str(tmp_path / 'remote-msg'))
    text, is_error = sess_a.mcp.call('remote_connect', alias='h1', host=sshd['host'], path=str(remote_dir))
    assert not is_error, text
    sess_a.mcp.call('remote_send', alias='h1', session=sess_b.sid, body='are you there')

    def state_of() -> str:
        return inbox_text(sess_a).split('--- sent ---\n')[1].split('\n')[1].split()[2]

    assert state_of() == 'waiting'
    inbox_text(sess_b)
    assert state_of() == 'read'
    sess_b.mcp.call('remote_send', session=sess_a.sid, body='yes')
    assert wait_for(lambda: state_of() == 'answered', 2 * POLL_INTERVAL_S + 2)


def test_registration_after_reload_plugins_needs_one_prompt(session: Any, tmp_path: Path) -> None:
    sess = session('p', hook=False)
    flag = sess.rt / 'need-session'
    assert wait_for(flag.exists, 5)
    assert not list((tmp_path / 'msg-p' / 'sessions').glob('*.json'))
    run_hook(sess.env, 'UserPromptSubmit', {'session_id': sess.sid, 'cwd': str(sess.cwd)})
    assert wait_for(lambda: (tmp_path / 'msg-p' / 'sessions' / f'{sess.sid}.json').exists(), 5)
    assert not flag.exists()
