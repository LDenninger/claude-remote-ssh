"""Unit tests for remote_ssh.relay: local store, routing, claiming and the remote shell snippets."""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from remote_ssh import relay
from remote_ssh.relay import Store


class FakeMaster:
    """Run remote commands locally through ``bash -lc``, recording each command."""

    def __init__(self) -> None:
        self.alias = 'gpu-a'
        self.commands: list[str] = []
        self.timeouts: list[float | None] = []
        self.before_run = None

    def run(self, command: str, cwd: str | None = None, input: bytes | None = None, timeout: float | None = 60) -> subprocess.CompletedProcess:
        """Execute ``command`` like the real master would on the remote."""
        if self.before_run is not None:
            self.before_run(command)
        self.commands.append(command)
        self.timeouts.append(timeout)
        return subprocess.run(['bash', '-lc', command], cwd=cwd, input=input, capture_output=True, timeout=timeout)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    monkeypatch.setenv('REMOTE_SSH_MSG_BASE', str(tmp_path / 'local'))
    return Store()


@pytest.fixture
def remote_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / 'remote base with space'
    monkeypatch.setenv('REMOTE_SSH_REMOTE_BASE', str(base))
    return base


def _msg(to: str, body: str = 'hi') -> dict:
    return relay.new_message('sender-0000', 'proj-send', 'laptop', to, body)


#---------------------------------------------------------------------
# Messages and lookup
#---------------------------------------------------------------------

def test_new_message_fields_and_sortable_ids() -> None:
    first = _msg('to-1')
    time.sleep(0.002)
    second = _msg('to-1')
    assert set(first) == {'id', 'from_session', 'from_name', 'from_host', 'to_session', 'sent_at', 'body'}
    assert relay.MSG_FILE_RE.match(f'{first["id"]}.json')
    assert first['id'] < second['id']
    assert first['sent_at'].endswith('+00:00')


def test_session_name_joins_directory_and_id_prefix() -> None:
    assert relay.session_name('/home/u/proj', 'abcdef12') == 'proj-abcd'
    assert relay.session_name('/', 'abcdef12') == 'root-abcd'


def test_resolve_by_name_id_and_prefix() -> None:
    sessions = [{'session_id': 'aaaa1111', 'name': 'proj-aaaa'}, {'session_id': 'aaab2222', 'name': 'other-aaab'}]
    assert relay.resolve(sessions, 'aaaa1111') is sessions[0]
    assert relay.resolve(sessions, 'other-aaab') is sessions[1]
    assert relay.resolve(sessions, 'aaab') is sessions[1]
    assert relay.resolve(sessions, 'aaa') is None
    assert relay.resolve(sessions, 'zzz') is None
    assert relay.resolve(sessions, '') is None


#---------------------------------------------------------------------
# Local store
#---------------------------------------------------------------------

def test_register_lists_and_unregister_removes_sessions(store: Store) -> None:
    store.register('sid-1', {'name': 'proj-sid-', 'pid': os.getpid()})
    assert store.sessions() == [{'name': 'proj-sid-', 'pid': os.getpid(), 'session_id': 'sid-1'}]
    assert stat.S_IMODE((store.base / 'sessions').stat().st_mode) == 0o700
    store.unregister('sid-1')
    store.unregister('sid-1')
    assert store.sessions() == []


def test_unsafe_ids_rejected(store: Store) -> None:
    with pytest.raises(ValueError):
        store.register('../escape', {})
    with pytest.raises(ValueError):
        store.claim('sid', '../../x')


def test_send_routes_inbox_vs_outbox(store: Store) -> None:
    store.register('local-sid', {'name': 'here'})
    store.register('relayed-sid', {'name': 'there', 'relay_via': 'laptop'})
    local_msg, relayed_msg = _msg('local-sid'), _msg('relayed-sid')
    assert store.send(local_msg) == 'inbox'
    assert store.send(relayed_msg) == 'outbox'
    assert store.pending('local-sid') == [local_msg]
    assert store.pending('relayed-sid') == []
    assert [path.name for path in store.outbox_files('relayed-sid')] == [f'{relayed_msg["id"]}.json']
    with pytest.raises(KeyError):
        store.send(_msg('unknown-sid'))


def test_deliver_is_atomic_and_private(store: Store) -> None:
    msg = _msg('sid')
    store.deliver(msg)
    inbox_dir = store.base / 'inbox' / 'sid'
    names = [path.name for path in inbox_dir.iterdir() if path.is_file()]
    assert names == [f'{msg["id"]}.json']
    assert stat.S_IMODE((inbox_dir / names[0]).stat().st_mode) == 0o600
    # a temp file left by a concurrent writer is never reported as a message
    (inbox_dir / f'.{msg["id"]}.json.x.tmp').write_text('{"partial')
    assert store.pending('sid') == [msg]


def test_claim_and_read_messages(store: Store) -> None:
    msgs = sorted((_msg('sid', f'body {ii}') for ii in range(3)), key=lambda msg: msg['id'])
    for msg in msgs:
        store.deliver(msg)
    assert store.claim('sid', msgs[0]['id']) == msgs[0]
    assert store.claim('sid', msgs[0]['id']) is None
    assert store.pending('sid') == msgs[1:]
    assert store.read_messages('sid') == msgs[:1]
    # a delivered message is not delivered twice
    store.deliver(msgs[0])
    assert store.pending('sid') == msgs[1:]


def test_claim_exactly_once_under_concurrency(tmp_path: Path) -> None:
    base = tmp_path / 'msgs'
    msgs = [{**_msg('sid', str(ii)), 'id': f'{1000 + ii}-{ii:08x}'} for ii in range(300)]
    for msg in msgs:
        Store(base).deliver(msg)
    won: list[list[str]] = [[] for _ in range(4)]
    barrier = threading.Barrier(4)

    def claim_all(slot: int) -> None:
        worker_store = Store(base)
        barrier.wait()
        for msg in msgs:
            claimed = worker_store.claim('sid', msg['id'])
            if claimed is not None:
                won[slot].append(claimed['id'])

    threads = [threading.Thread(target=claim_all, args=(ii,)) for ii in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    all_won = [msg_id for slot in won for msg_id in slot]
    assert sorted(all_won) == sorted(msg['id'] for msg in msgs)
    assert len(all_won) == len(set(all_won))


def test_claim_exactly_once_across_processes(tmp_path: Path) -> None:
    base = tmp_path / 'msgs'
    msgs = [{**_msg('sid', str(ii)), 'id': f'{2000 + ii}-{ii:08x}'} for ii in range(200)]
    for msg in msgs:
        Store(base).deliver(msg)
    script = ('import json, sys\nfrom pathlib import Path\nfrom remote_ssh.inbox import claim_pending\nfrom remote_ssh.relay import Store\n'
              'print(json.dumps([m["id"] for m in claim_pending(Store(Path(sys.argv[1])), "sid", limit=None)]))\n')
    repo_root = Path(__file__).resolve().parents[2]
    procs = [subprocess.Popen(['python3', '-c', script, str(base)], cwd=repo_root, stdout=subprocess.PIPE) for _ in range(3)]
    results = [json.loads(proc.communicate(timeout=60)[0]) for proc in procs]
    all_won = [msg_id for result in results for msg_id in result]
    assert sorted(all_won) == sorted(msg['id'] for msg in msgs)


def test_sweep_removes_dead_local_sessions_and_keeps_relayed_and_live_ones(store: Store, tmp_path: Path) -> None:
    dead = subprocess.Popen(['true'])
    dead.wait()
    host = socket.gethostname()
    store.register('dead-sid', {'pid': dead.pid, 'host': host})
    store.register('alive-sid', {'pid': os.getpid(), 'host': host})
    store.register('relay-sid', {'pid': dead.pid, 'host': host, 'relay_via': 'laptop'})
    store.register('live-sid', {'pid': dead.pid, 'host': host})
    store.deliver(_msg('gone-sid'))
    store.deliver(_msg('fresh-sid'))
    old = time.time() - 8 * 86400
    os.utime(store.base / 'inbox' / 'gone-sid', (old, old))
    store.sweep({'live-sid'})
    assert {entry['session_id'] for entry in store.sessions()} == {'alive-sid', 'relay-sid', 'live-sid'}
    assert not (store.base / 'inbox' / 'gone-sid').exists()
    assert (store.base / 'inbox' / 'fresh-sid').exists()


#---------------------------------------------------------------------
# Remote side
#---------------------------------------------------------------------

def test_remote_base_default_expands_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('REMOTE_SSH_REMOTE_BASE', raising=False)
    master = FakeMaster()
    relay.remote_sessions(master)
    assert '"$HOME/.claude/remote-ssh"/sessions/*.json' in master.commands[0]


def test_remote_register_lists_and_unregister_removes_sessions(remote_base: Path) -> None:
    master = FakeMaster()
    relay.remote_register(master, 'sid-a', {'name': 'proj-sid-', 'relay_via': 'laptop'})
    relay.remote_register(master, 'sid-b', {'name': "it's odd"})
    (remote_base / 'sessions' / 'broken.json').write_text('{not json')
    assert sorted(entry['session_id'] for entry in relay.remote_sessions(master)) == ['sid-a', 'sid-b']
    assert stat.S_IMODE((remote_base / 'sessions').stat().st_mode) == 0o700
    assert stat.S_IMODE((remote_base / 'sessions' / 'sid-a.json').stat().st_mode) == 0o600
    assert not [path for path in (remote_base / 'sessions').iterdir() if path.name.endswith('.tmp')]
    relay.remote_unregister(master, 'sid-a')
    assert [entry['session_id'] for entry in relay.remote_sessions(master)] == ['sid-b']


def test_remote_sessions_empty_when_missing(remote_base: Path) -> None:
    assert relay.remote_sessions(FakeMaster()) == []


def test_remote_deliver_lands_in_remote_inbox(remote_base: Path) -> None:
    msg = _msg('remote-sid', 'multi\nline "quoted" $HOME')
    relay.remote_deliver(FakeMaster(), msg)
    assert Store(remote_base).pending('remote-sid') == [msg]


def test_pull_outbox_round_trip(remote_base: Path, store: Store) -> None:
    remote_store = Store(remote_base)
    remote_store.register('local-sid', {'relay_via': 'laptop'})
    msgs = sorted((_msg('local-sid', f'from remote {ii}') for ii in range(3)), key=lambda msg: msg['id'])
    for msg in msgs:
        assert remote_store.send(msg) == 'outbox'
    late = {**_msg('local-sid', 'late'), 'id': f'{int(time.time() * 1000) + 1000}-0000000a'}
    master = FakeMaster()

    def add_late_file(command: str) -> None:
        if 'rm -f' in command:
            relay.state.write_json_atomic(remote_base / 'outbox' / 'local-sid' / f'{late["id"]}.json', late)

    master.before_run = add_late_file
    assert relay.pull_outbox(master, 'local-sid', store) == 3
    assert store.pending('local-sid') == [{**msg, 'relayed_from': 'gpu-a'} for msg in msgs]
    assert [path.name for path in remote_store.outbox_files('local-sid')] == [f'{late["id"]}.json']
    master.before_run = None
    assert relay.pull_outbox(master, 'local-sid', store) == 1
    assert store.pending('local-sid') == [{**msg, 'relayed_from': 'gpu-a'} for msg in msgs + [late]]
    assert remote_store.outbox_files('local-sid') == []
    assert relay.pull_outbox(master, 'local-sid', store) == 0


def test_pull_outbox_drops_misaddressed_and_renamed_messages(remote_base: Path, store: Store) -> None:
    outbox_dir = remote_base / 'outbox' / 'local-sid'
    outbox_dir.mkdir(parents=True)
    good = _msg('local-sid', 'fine')
    misaddressed = _msg('other-sid', 'meant for someone else')
    renamed = {**_msg('local-sid', 'id does not name the file'), 'id': '../../escape'}
    spoofed = {**_msg('local-sid', 'claims provenance'), 'relayed_from': 'trusted-host'}
    for file_name, msg in ((f'{good["id"]}.json', good), (f'{misaddressed["id"]}.json', misaddressed),
                           ('1-0000000a.json', renamed), (f'{spoofed["id"]}.json', spoofed)):
        relay.state.write_json_atomic(outbox_dir / file_name, msg)
    master = FakeMaster()
    assert relay.pull_outbox(master, 'local-sid', store, timeout=4) == 2
    assert sorted(msg['body'] for msg in store.pending('local-sid')) == ['claims provenance', 'fine']
    assert {msg['relayed_from'] for msg in store.pending('local-sid')} == {'gpu-a'}
    assert store.pending('other-sid') == []
    assert list(outbox_dir.glob('*.json')) == []
    assert set(master.timeouts) == {4}


def test_has_pending_follows_delivery_and_claim(store: Store) -> None:
    assert not store.has_pending('sid')
    msg = _msg('sid')
    store.deliver(msg)
    assert store.has_pending('sid')
    store.claim('sid', msg['id'])
    assert not store.has_pending('sid')


def test_remote_unregister_passes_timeout(remote_base: Path) -> None:
    master = FakeMaster()
    relay.remote_unregister(master, 'sid', timeout=3)
    assert master.timeouts == [3]


def test_pull_outbox_missing_dir(remote_base: Path, store: Store) -> None:
    assert relay.pull_outbox(FakeMaster(), 'nobody', store) == 0


def test_remote_cleanup_removes_stale_entries_of_this_machine(remote_base: Path) -> None:
    master = FakeMaster()
    relay.remote_register(master, 'live-sid', {'relay_via': 'laptop'})
    relay.remote_register(master, 'stale-sid', {'relay_via': 'laptop'})
    relay.remote_register(master, 'other-host-sid', {'relay_via': 'desktop'})
    relay.remote_register(master, 'native-sid', {'name': 'remote session'})
    relay.remote_cleanup(master, 'laptop', {'live-sid'})
    assert sorted(entry['session_id'] for entry in relay.remote_sessions(master)) == ['live-sid', 'native-sid', 'other-host-sid']


def test_remote_register_raises_when_the_command_fails(remote_base: Path) -> None:
    remote_base.mkdir()
    (remote_base / 'sessions').write_text('a file, not a directory')
    with pytest.raises(RuntimeError):
        relay.remote_register(FakeMaster(), 'sid', {})


#---------------------------------------------------------------------
# Shared home directories and bounded mailboxes
#---------------------------------------------------------------------

def _stamped_msg(body: str = 'hi', to: str = 'sid', **fields: str) -> dict:
    return {**relay.new_message('abcdef1234567890', 'proj-abcd', 'laptop', to, body), 'sent_at': '2026-10-11T12:00:00+00:00', **fields}


def test_sweep_checks_pids_only_of_this_node(tmp_path: Path) -> None:
    dead = subprocess.Popen(['true'])
    dead.wait()
    store = Store(tmp_path)
    store.register('mine-dead', {'pid': dead.pid, 'host': socket.gethostname()})
    store.register('other-node', {'pid': dead.pid, 'host': 'node-other'})
    store.sweep(set())
    assert {entry['session_id'] for entry in store.sessions()} == {'other-node'}


def test_remote_cleanup_keeps_entries_of_other_nodes(remote_base: Path) -> None:
    master = FakeMaster()
    relay.remote_register(master, 'mine-stale', {'relay_via': 'node1-id'})
    relay.remote_register(master, 'other-node', {'relay_via': 'node2-id'})
    relay.remote_cleanup(master, 'node1-id', set())
    assert [entry['session_id'] for entry in relay.remote_sessions(master)] == ['other-node']


def test_read_directory_keeps_most_recent_200(tmp_path: Path) -> None:
    store = Store(tmp_path)
    for ii in range(205):
        msg = {**_stamped_msg(str(ii)), 'id': f'{1000 + ii}-{ii:08x}'}
        store.deliver(msg)
        store.claim('sid', msg['id'])
    kept = sorted(path.name for path in (tmp_path / 'inbox' / 'sid' / 'read').glob('*.json'))
    assert len(kept) == relay.MAX_READ_KEPT == 200
    assert kept[0].startswith('1005-') and kept[-1].startswith('1204-')


def test_pull_outbox_caps_messages_per_poll(remote_base: Path, tmp_path: Path) -> None:
    remote_store, local = relay.Store(remote_base), Store(tmp_path / 'local')
    remote_store.register('local-sid', {'relay_via': 'm'})
    for ii in range(130):
        remote_store.send({**_stamped_msg('x', to='local-sid'), 'id': f'{1000 + ii}-{ii:08x}'})
    master = FakeMaster()
    assert relay.pull_outbox(master, 'local-sid', local) == 100
    assert len(remote_store.outbox_files('local-sid')) == 30
    assert relay.pull_outbox(master, 'local-sid', local) == 30
    assert remote_store.outbox_files('local-sid') == []


def test_pull_outbox_caps_bytes_per_poll(remote_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay, 'MAX_PULL_BYTES', 4000)
    remote_store, local = relay.Store(remote_base), Store(tmp_path / 'local')
    remote_store.register('local-sid', {'relay_via': 'm'})
    for ii in range(5):
        remote_store.send({**_stamped_msg('y' * 1500, to='local-sid'), 'id': f'{1000 + ii}-{ii:08x}'})
    master = FakeMaster()
    first = relay.pull_outbox(master, 'local-sid', local)
    assert 0 < first < 5
    assert len(remote_store.outbox_files('local-sid')) == 5 - first
    assert relay.pull_outbox(master, 'local-sid', local) > 0


def test_pull_outbox_drops_a_file_larger_than_the_budget(remote_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay, 'MAX_PULL_BYTES', 1000)
    remote_store, local = relay.Store(remote_base), Store(tmp_path / 'local')
    remote_store.register('local-sid', {'relay_via': 'm'})
    remote_store.send({**_stamped_msg('z' * 5000, to='local-sid'), 'id': '1000-00000000'})
    remote_store.send({**_stamped_msg('ok', to='local-sid'), 'id': '1001-00000001'})
    assert relay.pull_outbox(FakeMaster(), 'local-sid', local) == 1
    assert remote_store.outbox_files('local-sid') == []


#---------------------------------------------------------------------
# Long bodies reach the recipient whole
#---------------------------------------------------------------------

@pytest.mark.parametrize('chars', [1800, 7900])
def test_remote_deliver_keeps_long_bodies_with_newlines_and_quotes(remote_base: Path, chars: int) -> None:
    line = 'He said "stop" and it\'s `$HOME` \\n done; rm -rf * # \'\'\' """\n'
    body = (line * (chars // len(line) + 1))[:chars]
    master = FakeMaster()
    msg = _msg('peer-0001', body)
    relay.remote_deliver(master, msg)
    stored = json.loads((remote_base / 'inbox' / 'peer-0001' / f'{msg["id"]}.json').read_text())
    assert stored['body'] == body


#---------------------------------------------------------------------
# Stable names across resume
#---------------------------------------------------------------------

def test_resolve_follows_previous_ids() -> None:
    sessions = [{'session_id': 'new-0002', 'name': 'proj-old1', 'previous_ids': ['old-0001']}]
    assert relay.resolve(sessions, 'old-0001') is sessions[0]
    assert relay.resolve(sessions, 'proj-old1') is sessions[0]
    assert relay.resolve(sessions, 'gone-0000') is None


def _entry(cwd: str = '/w/proj', name: str = 'proj-old1', pid: int | None = None, **fields: object) -> dict:
    return {'name': name, 'cwd': cwd, 'host': socket.gethostname(), 'pid': pid if pid is not None else os.getpid(), 'started': '2026-10-11T10:00:00+00:00', **fields}


def test_unregister_retires_the_entry_and_register_drops_the_copy(store: Store) -> None:
    store.register('old-0001', _entry())
    store.unregister('old-0001')
    assert store.sessions() == [] and (store.base / 'ended' / 'old-0001.json').exists()
    store.register('old-0001', _entry())
    assert not (store.base / 'ended' / 'old-0001.json').exists()
    store.unregister('old-0001', retire=False)
    assert not (store.base / 'ended').exists() or not list((store.base / 'ended').glob('*.json'))


def test_take_over_claims_the_name_of_a_session_that_ended_in_the_same_directory(store: Store) -> None:
    store.register('old-0001', _entry())
    store.unregister('old-0001')
    taken = store.take_over('/w/proj', 'new-0002')
    assert taken == {'name': 'proj-old1', 'previous_ids': ['old-0001']}
    assert store.take_over('/w/proj', 'new-0003') is None


def test_take_over_chains_previous_ids(store: Store) -> None:
    store.register('mid-0002', _entry(name='proj-old1', previous_ids=['old-0001']))
    store.unregister('mid-0002')
    assert store.take_over('/w/proj', 'new-0003')['previous_ids'] == ['old-0001', 'mid-0002']


def test_take_over_claims_a_dead_registration_and_leaves_live_and_foreign_ones(store: Store) -> None:
    dead = subprocess.Popen(['true'])
    dead.wait()
    store.register('dead-0001', _entry(pid=dead.pid, name='proj-dead'))
    store.register('live-0002', _entry(name='proj-live'))
    store.register('other-0003', _entry(cwd='/w/other', pid=dead.pid, name='other-dead'))
    store.register('relay-0004', {**_entry(pid=dead.pid, name='proj-rel'), 'relay_via': 'machine-b'})
    assert store.take_over('/w/proj', 'new-0005') == {'name': 'proj-dead', 'previous_ids': ['dead-0001']}
    assert sorted(entry['session_id'] for entry in store.sessions()) == ['live-0002', 'other-0003', 'relay-0004']
    assert store.take_over('/w/proj', 'new-0006') is None


def test_take_over_never_claims_its_own_entry(store: Store) -> None:
    store.register('same-0001', _entry())
    store.unregister('same-0001')
    assert store.take_over('/w/proj', 'same-0001') is None


def test_move_inbox_readdresses_unread_messages(store: Store) -> None:
    store.deliver(_msg('old-0001', 'unread'))
    store.move_inbox('old-0001', 'new-0002')
    assert not store.has_pending('old-0001')
    assert [(msg['to_session'], msg['body']) for msg in store.pending('new-0002')] == [('new-0002', 'unread')]


#---------------------------------------------------------------------
# Sent log and delivery states
#---------------------------------------------------------------------

def test_sent_log_keeps_the_latest_hundred_of_one_sender(store: Store) -> None:
    for ii in range(103):
        msg = {**_msg('peer-0001', f'body {ii}'), 'id': f'{1000 + ii}-{ii:08x}'}
        store.log_sent(msg, 'peer', None, 'inbox')
    store.log_sent({**_msg('peer-0001'), 'from_session': 'someone-else', 'id': '9999-00000000'}, 'peer', None, 'inbox')
    log = store.sent_log('sender-0000', limit=500)
    assert len(log) == 99 and log[-1]['preview'] == 'body 102'
    assert len(list((store.base / 'sent').glob('*.json'))) == 100


def test_local_receipt_follows_the_message(store: Store) -> None:
    store.register('peer-0001', {'name': 'peer'})
    msg = _msg('peer-0001')
    store.send(msg)
    store.log_sent(msg, 'peer', None, 'inbox')
    [entry] = store.sent_log('sender-0000')
    assert store.local_receipt(entry) == 'waiting'
    store.claim('peer-0001', msg['id'])
    assert store.local_receipt(entry) == 'read'
    #--- the recipient was resumed under a new id and its inbox moved with it ---
    moved = _msg('peer-0001')
    store.send(moved)
    store.unregister('peer-0001')
    store.register('peer-0002', {'name': 'peer', 'previous_ids': ['peer-0001']})
    store.move_inbox('peer-0001', 'peer-0002')
    assert store.local_receipt({'id': moved['id'], 'to_session': 'peer-0001', 'route': 'inbox'}) == 'waiting'
    assert store.local_receipt({'id': moved['id'], 'to_session': 'gone-0009', 'route': 'inbox'}) == 'unknown'


def test_local_receipt_of_an_outbox_message(store: Store) -> None:
    store.register('far-0001', {'name': 'far', 'relay_via': 'machine-b'})
    msg = _msg('far-0001')
    assert store.send(msg) == 'outbox'
    entry = {'id': msg['id'], 'to_session': 'far-0001', 'route': 'outbox'}
    assert store.local_receipt(entry) == 'waiting'
    store.outbox_files('far-0001')[0].unlink()
    assert store.local_receipt(entry) == 'delivered'


def test_remote_receipts_take_one_round_trip(remote_base: Path) -> None:
    remote = Store(remote_base)
    remote.register('peer-0001', {'name': 'peer'})
    remote.register('far-0002', {'name': 'far', 'relay_via': 'machine-b'})
    waiting, read, outbox = _msg('peer-0001'), _msg('peer-0001'), _msg('far-0002')
    remote.send(waiting)
    remote.send(read)
    remote.claim('peer-0001', read['id'])
    remote.send(outbox)
    entries = [{'id': waiting['id'], 'to_session': 'peer-0001', 'route': 'inbox'}, {'id': read['id'], 'to_session': 'peer-0001', 'route': 'inbox'},
               {'id': 'nope-00000000', 'to_session': 'peer-0001', 'route': 'inbox'}, {'id': outbox['id'], 'to_session': 'far-0002', 'route': 'outbox'}]
    master = FakeMaster()
    assert relay.remote_receipts(master, entries) == ['waiting', 'read', 'unknown', 'waiting']
    assert len(master.commands) == 1
    assert relay.remote_receipts(master, []) == [] and len(master.commands) == 1


#---------------------------------------------------------------------
# Attachments
#---------------------------------------------------------------------

def test_check_attachments_names_and_limits(tmp_path: Path) -> None:
    (tmp_path / 'sub').mkdir()
    for name in ('a.txt', 'sub/a.txt', '.hidden', 'we ird$name.bin'):
        (tmp_path / name).write_text('x')
    files = relay.check_attachments(['a.txt', 'sub/a.txt', '.hidden', 'we ird$name.bin'], str(tmp_path))
    assert [name for _, name in files] == ['a.txt', '1-a.txt', 'hidden', 'we_ird_name.bin']
    for bad, text in ((['missing.txt'], 'does not exist'), (['sub'], 'not a regular file'), ('a.txt', 'must be a list'), ([''], 'must be a list'),
                      (['a.txt'] * (relay.MAX_ATTACH_FILES + 1), 'the limit is 10')):
        with pytest.raises(ValueError, match=text):
            relay.check_attachments(bad, str(tmp_path))
    (tmp_path / 'big.bin').write_bytes(b'x' * (relay.MAX_ATTACH_BYTES + 1))
    with pytest.raises(ValueError, match='total'):
        relay.check_attachments(['big.bin'], str(tmp_path))


def test_remote_attach_returns_the_remote_paths(remote_base: Path, tmp_path: Path) -> None:
    (tmp_path / 'data.bin').write_bytes(bytes(range(256)) * 10)
    files = relay.check_attachments(['data.bin'], str(tmp_path))
    [path] = relay.remote_attach(FakeMaster(), 'msg-0001', files)
    assert Path(path) == remote_base / 'attachments' / 'msg-0001' / 'data.bin'
    assert Path(path).read_bytes() == bytes(range(256)) * 10


def test_attachments_for_a_relayed_recipient_are_pulled_with_the_message(remote_base: Path, store: Store, tmp_path: Path) -> None:
    (tmp_path / 'img.bin').write_bytes(b'\x00\xff\n\x00data')
    files = relay.check_attachments(['img.bin'], str(tmp_path))
    msg = {**_msg('here-0001', 'see file'), 'attachment_names': ['img.bin'], 'attachments': ['/etc/passwd']}
    master = FakeMaster()
    relay.remote_attach(master, msg['id'], files, to_sid='here-0001')
    relay.remote_deliver(master, msg, 'outbox')
    assert relay.pull_outbox(master, 'here-0001', store) == 1
    [pulled] = store.pending('here-0001')
    [local_path] = pulled['attachments']
    assert Path(local_path) == store.base / 'attachments' / msg['id'] / 'img.bin'
    assert Path(local_path).read_bytes() == b'\x00\xff\n\x00data'
    assert 'attachment_names' not in pulled
    assert not (remote_base / 'outbox' / 'here-0001' / 'attachments' / msg['id']).exists()
