"""Message store and transport between Claude sessions.

Every machine keeps its messaging state under one base directory: ``sessions/<sid>.json`` for the
registered sessions, ``inbox/<sid>/`` for undelivered messages (``read/`` for delivered ones) and
``outbox/<sid>/`` for messages waiting to be pulled to another machine. ``Store`` operates on the
local base. The ``remote_*`` functions and ``pull_outbox`` operate on a remote base through a
``conn.Master`` and assume only POSIX sh and coreutils on the remote side. Every write is a temp
file followed by a rename, so a reader never sees a partial message.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from remote_ssh import state

ID_RE = re.compile(r'^[A-Za-z0-9-]{1,128}$')
MSG_FILE_RE = re.compile(r'^\d+-[0-9a-f]{8}\.json$')
REMOTE_TIMEOUT_S = 30
MAX_BODY_CHARS = 8000
MAX_READ_KEPT = 200
MAX_PULL_MESSAGES = 100
MAX_PULL_BYTES = 1024 * 1024
FIELD_CAP = 64
MAX_ATTACH_BYTES = 5 * 1024 * 1024
MAX_ATTACH_FILES = 10
ATTACH_TIMEOUT_S = 120
MAX_SENT_KEPT = 100
SSH_ERROR_EXIT = 255
MAX_PREVIOUS_IDS = 20
ENDED_MAX_AGE_S = 7 * 86400
NAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,100}$')


#---------------------------------------------------------------------
# Messages and session lookup
#---------------------------------------------------------------------

def new_message(from_session: str, from_name: str, from_host: str, to_session: str, body: str) -> dict:
    """Build a message dict with a fresh, time-sortable id.

    Args:
        from_session: Session id of the sender.
        from_name: Display name of the sender.
        from_host: Hostname of the sender's machine.
        to_session: Session id of the recipient.
        body: Message text.

    Returns:
        The message, with ``id`` and an ISO 8601 UTC ``sent_at``.
    """
    return {
        'id': f'{int(time.time() * 1000)}-{secrets.token_hex(4)}',
        'from_session': from_session,
        'from_name': from_name,
        'from_host': from_host,
        'to_session': to_session,
        'sent_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'body': body,
    }


def clean_text(value: Any, cap: int = FIELD_CAP) -> str:
    """Return ``value`` as one printable line of at most ``cap`` characters.

    Fields that come from a remote file are untrusted. Newlines and other control or format
    characters become spaces, so a field can never open a line of its own in Claude's context.

    Args:
        value: Any value, converted with ``str``.
        cap: Maximum length of the result.

    Returns:
        The sanitized text.
    """
    return ''.join(char if char.isprintable() else ' ' for char in str(value))[:cap]


def session_name(cwd: str, sid: str) -> str:
    """Return the display name ``<cwd basename>-<first 4 chars of sid>``."""
    return f'{os.path.basename(cwd.rstrip("/")) or "root"}-{sid[:4]}'


def resolve(sessions: list[dict], name_or_id: str) -> dict | None:
    """Find a session by exact id, exact name, an id it replaced, or unique id prefix.

    Args:
        sessions: Session entries, each with ``session_id``, ``name`` and optionally ``previous_ids``.
        name_or_id: The id, the name, an earlier id of a resumed session or a prefix of the id.

    Returns:
        The matching entry, or None when nothing matches or the prefix is ambiguous.
    """
    for key in ('session_id', 'name'):
        for entry in sessions:
            if entry.get(key) == name_or_id:
                return entry
    if not name_or_id:
        return None
    for entry in sessions:
        if name_or_id in (entry.get('previous_ids') or []):
            return entry
    matches = [entry for entry in sessions if str(entry.get('session_id', '')).startswith(name_or_id)]
    return matches[0] if len(matches) == 1 else None


def _check_id(value: str) -> str:
    """Return ``value`` if it is safe as a path component, else raise ValueError."""
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ValueError(f'unsafe id: {value!r}')
    return value


def check_attachments(paths: Any, base_dir: str = '') -> list[tuple[Path, str]]:
    """Validate files to attach and choose the name each is stored under.

    A stored name is the file's base name reduced to ``A-Za-z0-9._-`` without a leading dot, so no
    path component of the source reaches the recipient's disk. Equal names get a numeric prefix.

    Args:
        paths: The ``files`` argument, expected to be a list of path strings.
        base_dir: Directory a relative path is taken from.

    Returns:
        ``(source, stored name)`` pairs in the order given.

    Raises:
        ValueError: A path is not a string, does not exist, is not a regular file, there are more
            than ``MAX_ATTACH_FILES`` files, or they total more than ``MAX_ATTACH_BYTES``.
    """
    if not isinstance(paths, list) or not all(isinstance(path, str) and path for path in paths):
        raise ValueError('files must be a list of paths')
    if len(paths) > MAX_ATTACH_FILES:
        raise ValueError(f'{len(paths)} files, the limit is {MAX_ATTACH_FILES}')
    files, total, used = [], 0, set()
    for raw in paths:
        source = Path(base_dir, os.path.expanduser(raw)).resolve()
        if not source.exists():
            raise ValueError(f'{raw} does not exist')
        if not source.is_file():
            raise ValueError(f'{raw} is not a regular file')
        total += source.stat().st_size
        name = re.sub(r'[^A-Za-z0-9._-]', '_', source.name).lstrip('.')[:80] or 'file'
        while name in used:
            name = f'{len(used)}-{name}'
        used.add(name)
        files.append((source, name))
    if total > MAX_ATTACH_BYTES:
        raise ValueError(f'the files total {total} bytes, the limit is {MAX_ATTACH_BYTES}')
    return files


#---------------------------------------------------------------------
# Local store
#---------------------------------------------------------------------

class Store:
    """The messaging directory of the local machine.

    Args:
        base: Root of the messaging directory. Defaults to ``state.msg_base()``.

    Attributes:
        base: Root of the messaging directory.
    """

    def __init__(self, base: Path | None = None) -> None:
        self.base = Path(base) if base is not None else state.msg_base()

    def __ensure_dir(self, *parts: str) -> Path:
        """Return ``base/<parts>``, creating every level as a private directory."""
        path = state.ensure_private_dir(self.base)
        for part in parts:
            path = state.ensure_private_dir(path / part)
        return path

    def register(self, sid: str, entry: dict) -> None:
        """Write the session entry ``sessions/<sid>.json``, adding ``session_id``, and drop its retired copy."""
        state.write_json_atomic(self.__ensure_dir('sessions') / f'{_check_id(sid)}.json', {**entry, 'session_id': sid})
        (self.base / 'ended' / f'{sid}.json').unlink(missing_ok=True)

    def unregister(self, sid: str, retire: bool = True) -> None:
        """Remove the session entry of ``sid`` if present.

        Args:
            sid: Session id.
            retire: Keep the entry under ``ended/`` so a later session in the same directory can
                take over its name with ``take_over``. Entries relayed from another machine are
                never kept.
        """
        path = self.base / 'sessions' / f'{_check_id(sid)}.json'
        entry = state.read_json(path)
        if retire and isinstance(entry, dict) and not entry.get('relay_via'):
            state.write_json_atomic(self.__ensure_dir('ended') / path.name, {**entry, 'ended_at': time.time()})
        path.unlink(missing_ok=True)

    def take_over(self, cwd: str, sid: str) -> dict | None:
        """Claim the name of the newest ended session of this machine that ran in ``cwd``.

        A candidate is a retired entry or a registered one whose process is dead, so a resumed or
        cleared session keeps the name its predecessor had. Each candidate is claimed once.

        Args:
            cwd: Working directory of the registering session.
            sid: Session id of the registering session, which never takes over its own entry.

        Returns:
            ``name`` and ``previous_ids`` (the claimed ids, the claimed session's own last) for the
            new registration, or None when no session ended in ``cwd``.
        """
        hostname = socket.gethostname()
        ended_dir = self.base / 'ended'
        found = [(entry.get('ended_at', 0), entry, True) for entry in
                 (state.read_json(path) for path in sorted(ended_dir.glob('*.json')))]
        found += [(0, entry, False) for entry in self.sessions() if not is_pid_alive(entry.get('pid')) and not entry.get('relay_via')]
        candidates = [(moment, entry, retired) for moment, entry, retired in found
                      if isinstance(entry, dict) and entry.get('cwd') == cwd and entry.get('host') == hostname
                      and entry.get('session_id') != sid and ID_RE.match(str(entry.get('session_id')))]
        for _, entry, retired in sorted(candidates, key=lambda item: (item[0], str(item[1].get('started', ''))), reverse=True):
            old = entry['session_id']
            try:
                if retired:
                    (ended_dir / f'{old}.json').unlink()
                else:
                    self.unregister(old, retire=False)
            except FileNotFoundError:
                continue
            return {'name': entry.get('name'), 'previous_ids': [*(entry.get('previous_ids') or [])[-MAX_PREVIOUS_IDS:], old]}
        return None

    def move_inbox(self, old_sid: str, new_sid: str) -> None:
        """Move the unread messages of ``old_sid`` into the inbox of ``new_sid``, addressed to it."""
        for msg in self.pending(old_sid):
            self.deliver({**msg, 'to_session': new_sid})
            (self.base / 'inbox' / old_sid / f'{msg["id"]}.json').unlink(missing_ok=True)

    def sessions(self) -> list[dict]:
        """Return every registered session entry, skipping unreadable files."""
        sessions_dir = self.base / 'sessions'
        if not sessions_dir.is_dir():
            return []
        entries = [state.read_json(path) for path in sorted(sessions_dir.glob('*.json'))]
        return [entry for entry in entries if isinstance(entry, dict)]

    def send(self, msg: dict) -> str:
        """Place a message for a session registered on this machine.

        A target carrying ``relay_via`` lives on another machine and is reached through the outbox.

        Args:
            msg: Message from ``new_message``.

        Returns:
            ``"outbox"`` or ``"inbox"``, naming where the message was written.

        Raises:
            KeyError: The target session is not registered here.
        """
        target = next((entry for entry in self.sessions() if entry.get('session_id') == msg['to_session']), None)
        if target is None:
            raise KeyError(msg['to_session'])
        if target.get('relay_via'):
            msg_dir = self.__ensure_dir('outbox', _check_id(msg['to_session']))
            state.write_json_atomic(msg_dir / f'{_check_id(msg["id"])}.json', msg)
            return 'outbox'
        self.deliver(msg)
        return 'inbox'

    def deliver(self, msg: dict) -> None:
        """Write a message into the inbox of its recipient, unless it was already delivered."""
        inbox_dir = self.__ensure_dir('inbox', _check_id(msg['to_session']))
        file_name = f'{_check_id(msg["id"])}.json'
        if (inbox_dir / 'read' / file_name).exists():
            return
        state.write_json_atomic(inbox_dir / file_name, msg)

    def has_pending(self, sid: str) -> bool:
        """Return whether the inbox of ``sid`` holds an undelivered message, without parsing any."""
        return bool(self.__list_msg_files(self.base / 'inbox' / _check_id(sid)))

    def __list_msg_files(self, msg_dir: Path) -> list[Path]:
        """Return the message files in ``msg_dir`` sorted by id, ignoring temp files."""
        if not msg_dir.is_dir():
            return []
        return sorted(path for path in msg_dir.iterdir() if MSG_FILE_RE.match(path.name))

    def pending(self, sid: str) -> list[dict]:
        """Return the undelivered messages of ``sid`` sorted by id."""
        msgs = [state.read_json(path) for path in self.__list_msg_files(self.base / 'inbox' / _check_id(sid))]
        return [msg for msg in msgs if isinstance(msg, dict)]

    def claim(self, sid: str, msg_id: str) -> dict | None:
        """Move one message into ``read/`` and return it, if this caller wins the move.

        The rename is atomic, so among concurrent claimers of the same message exactly one gets it.
        Delivered messages beyond the most recent ``MAX_READ_KEPT`` are deleted.

        Args:
            sid: Recipient session id.
            msg_id: Id of the message to claim.

        Returns:
            The message, or None when another claimer moved it first.
        """
        read_dir = self.__ensure_dir('inbox', _check_id(sid), 'read')
        file_name = f'{_check_id(msg_id)}.json'
        try:
            os.rename(read_dir.parent / file_name, read_dir / file_name)
        except FileNotFoundError:
            return None
        msg = state.read_json(read_dir / file_name)
        for old_path in self.__list_msg_files(read_dir)[:-MAX_READ_KEPT]:
            old_path.unlink(missing_ok=True)
        return msg

    def read_messages(self, sid: str, limit: int = 50) -> list[dict]:
        """Return up to ``limit`` most recent delivered messages of ``sid``, oldest first."""
        paths = self.__list_msg_files(self.base / 'inbox' / _check_id(sid) / 'read')[-limit:] if limit > 0 else []
        msgs = [state.read_json(path) for path in paths]
        return [msg for msg in msgs if isinstance(msg, dict)]

    def outbox_files(self, sid: str) -> list[Path]:
        """Return the message files waiting in the outbox of ``sid``, sorted by id."""
        return self.__list_msg_files(self.base / 'outbox' / _check_id(sid))

    def log_sent(self, msg: dict, to_name: str, alias: str | None, route: str) -> None:
        """Record a sent message in ``sent/`` so ``remote_inbox`` can report its delivery state.

        Args:
            msg: The message that was sent.
            to_name: Display name of the recipient.
            alias: Host alias the message went through, None for a session on this machine.
            route: ``"inbox"`` or ``"outbox"``, where the message was written.
        """
        sent_dir = self.__ensure_dir('sent')
        body = clean_text(msg.get('body', ''), 48)
        state.write_json_atomic(sent_dir / f'{_check_id(msg["id"])}.json', {
            'id': msg['id'], 'from_session': msg['from_session'], 'to_session': msg['to_session'], 'to_name': to_name,
            'alias': alias, 'route': route, 'sent_at': msg['sent_at'], 'preview': body})
        for old_path in self.__list_msg_files(sent_dir)[:-MAX_SENT_KEPT]:
            old_path.unlink(missing_ok=True)

    def sent_log(self, from_sid: str, limit: int = 10) -> list[dict]:
        """Return the ``limit`` most recent messages ``from_sid`` sent, oldest first."""
        entries = [state.read_json(path) for path in self.__list_msg_files(self.base / 'sent')]
        return [entry for entry in entries if isinstance(entry, dict) and entry.get('from_session') == from_sid][-limit:]

    def local_receipt(self, entry: dict) -> str:
        """Return where a message logged by ``log_sent`` stands on this machine.

        Returns:
            ``"waiting"`` (unread in the recipient's inbox or not yet pulled from the outbox),
            ``"read"`` (moved to the recipient's ``read/``), ``"delivered"`` (left the outbox, so
            another machine pulled it) or ``"unknown"`` (the message is nowhere, for instance
            after the recipient's directory was swept).
        """
        file_name = f'{_check_id(entry["id"])}.json'
        if entry.get('route') == 'outbox':
            return 'waiting' if (self.base / 'outbox' / _check_id(entry['to_session']) / file_name).exists() else 'delivered'
        current = resolve(self.sessions(), entry['to_session'])
        for sid in dict.fromkeys([entry['to_session'], current['session_id'] if current else entry['to_session']]):
            if (self.base / 'inbox' / _check_id(sid) / file_name).exists():
                return 'waiting'
            if (self.base / 'inbox' / _check_id(sid) / 'read' / file_name).exists():
                return 'read'
        return 'unknown'

    def attach(self, msg_id: str, files: list[tuple[str, bytes]], box: str = 'attachments') -> list[str]:
        """Write files into ``<box>/<msg_id>/`` of this store.

        Args:
            msg_id: Id of the message the files belong to.
            files: ``(stored name, content)`` pairs, the names from ``check_attachments``.
            box: ``"attachments"`` for a recipient on this machine, ``"outbox/<sid>/attachments"``
                for one that pulls from this machine's outbox.

        Returns:
            The absolute paths of the copies.
        """
        target = self.__ensure_dir(*(_check_id(part) for part in box.split('/')), _check_id(msg_id))
        paths = []
        for name, content in files:
            state.write_bytes_atomic(target / name, content)
            paths.append(str(target / name))
        return paths

    def sweep(self, live_sids: set[str], max_age_s: float = 7 * 86400) -> None:
        """Drop registrations of dead sessions of this node and stale message directories.

        The pid of an entry is checked only when its ``host`` is this node's hostname, because a
        base on a shared home directory also holds the entries of other nodes.

        Args:
            live_sids: Session ids known to be alive. They are never swept.
            max_age_s: Minimum age of an inbox or outbox directory of an unregistered session
                before it is deleted. Defaults to seven days.
        """
        hostname = socket.gethostname()
        for entry in self.sessions():
            sid = entry.get('session_id')
            if sid in live_sids or entry.get('relay_via') or entry.get('host') != hostname or not ID_RE.match(str(sid)):
                continue
            if not is_pid_alive(entry.get('pid')):
                self.unregister(sid)
        registered = {entry.get('session_id') for entry in self.sessions()} | set(live_sids)
        cutoff = time.time() - max_age_s
        for kind in ('inbox', 'outbox'):
            kind_dir = self.base / kind
            if not kind_dir.is_dir():
                continue
            for msg_dir in kind_dir.iterdir():
                if msg_dir.name not in registered and msg_dir.is_dir() and msg_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(msg_dir, ignore_errors=True)
        for stale in [*(self.base / 'ended').glob('*.json'), *(self.base / 'attachments').glob('*')]:
            if stale.stat().st_mtime < cutoff:
                if stale.is_dir():
                    shutil.rmtree(stale, ignore_errors=True)
                else:
                    stale.unlink(missing_ok=True)


def is_pid_alive(pid: Any) -> bool:
    """Return whether ``pid`` names a running process. A missing or invalid pid counts as alive."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


#---------------------------------------------------------------------
# Remote side over a ControlMaster
#---------------------------------------------------------------------

class RemoteCommandError(RuntimeError):
    """A remote command exited non-zero. Exit code 255 means ssh itself failed.

    Args:
        returncode: Exit code of the local ssh process.
        stderr: Decoded, stripped stderr of the command.

    Attributes:
        returncode: Exit code of the local ssh process.
    """

    def __init__(self, returncode: int, stderr: str) -> None:
        super().__init__(f'remote command failed ({returncode}): {stderr}')
        self.returncode = returncode


def _get_remote_base() -> str:
    """Return the remote messaging base as a shell word.

    ``REMOTE_SSH_REMOTE_BASE`` overrides it and is quoted. The default stays double-quoted so the
    remote shell expands ``$HOME``.
    """
    override = os.environ.get('REMOTE_SSH_REMOTE_BASE')
    return shlex.quote(override) if override else '"$HOME/.claude/remote-ssh"'


def _run(master: Any, command: str, input: bytes | None = None, timeout: float = REMOTE_TIMEOUT_S) -> bytes:
    """Run ``command`` through ``master`` under plain ``sh`` and return stdout.

    Raises:
        RemoteCommandError: The command exited non-zero.
        subprocess.TimeoutExpired: The command outlived ``timeout`` seconds.
    """
    result = master.run(f'umask 077; {command}', input=input, timeout=timeout)
    if result.returncode != 0:
        raise RemoteCommandError(result.returncode, result.stderr.decode('utf-8', 'replace').strip())
    return result.stdout


def _build_write_cmd(dir_expr: str, file_name: str) -> str:
    """Return a shell command that writes stdin atomically to ``<dir>/<file_name>``."""
    tmp = shlex.quote(f'.{file_name}.tmp')
    final = shlex.quote(file_name)
    return f'd={dir_expr}; mkdir -p -m 700 "$d" && cat > "$d"/{tmp} && mv -f "$d"/{tmp} "$d"/{final}'


def _split_nul(data: bytes) -> list[bytes]:
    """Split NUL-terminated records."""
    return data.split(b'\0')[:-1] if data else []


def remote_register(master: Any, sid: str, entry: dict) -> None:
    """Write ``sessions/<sid>.json`` on the remote, adding ``session_id``."""
    payload = json.dumps({**entry, 'session_id': sid}).encode()
    _run(master, _build_write_cmd(f'{_get_remote_base()}/sessions', f'{_check_id(sid)}.json'), input=payload)


def remote_unregister(master: Any, sid: str, timeout: float = REMOTE_TIMEOUT_S) -> None:
    """Remove ``sessions/<sid>.json`` on the remote if present, giving up after ``timeout`` seconds."""
    _run(master, f'rm -f -- {_get_remote_base()}/sessions/{shlex.quote(_check_id(sid) + ".json")}', timeout=timeout)


def remote_sessions(master: Any, timeout: float = REMOTE_TIMEOUT_S) -> list[dict]:
    """Return every session entry registered on the remote, skipping unreadable files."""
    command = f'for f in {_get_remote_base()}/sessions/*.json; do [ -f "$f" ] && cat -- "$f" && printf "\\0"; done; true'
    entries = []
    for record in _split_nul(_run(master, command, timeout=timeout)):
        try:
            entry = json.loads(record)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def remote_deliver(master: Any, msg: dict, box: str = 'inbox') -> None:
    """Write a message atomically into the remote ``inbox`` or ``outbox`` directory of its recipient."""
    box_expr = f'{_get_remote_base()}/{box}/{shlex.quote(_check_id(msg["to_session"]))}'
    _run(master, _build_write_cmd(box_expr, f'{_check_id(msg["id"])}.json'), input=json.dumps(msg).encode())


def remote_attach(master: Any, msg_id: str, files: list[tuple[Path, str]], to_sid: str | None = None) -> list[str]:
    """Copy files to ``attachments/<msg_id>/`` on the remote, or into the outbox area of ``to_sid``.

    Args:
        master: Connection to the remote.
        msg_id: Id of the message the files belong to.
        files: ``(source, stored name)`` pairs from ``check_attachments``.
        to_sid: Recipient that pulls from the remote's outbox. None for a recipient on the remote.

    Returns:
        The absolute paths of the copies on the remote, in order.
    """
    area = f'outbox/{shlex.quote(_check_id(to_sid))}/attachments' if to_sid else 'attachments'
    dir_expr = f'{_get_remote_base()}/{area}/{shlex.quote(_check_id(msg_id))}'
    paths = []
    for source, name in files:
        command = _build_write_cmd(dir_expr, name) + f' && cd "$d" && printf "%s/%s" "$(pwd -P)" {shlex.quote(name)}'
        paths.append(_run(master, command, input=source.read_bytes(), timeout=ATTACH_TIMEOUT_S).decode('utf-8', 'replace'))
    return paths


def remote_receipts(master: Any, entries: list[dict], timeout: float = 5) -> list[str]:
    """Return the delivery state of logged messages on the remote, in one round trip.

    Args:
        master: Connection to the remote.
        entries: Records from ``Store.sent_log`` whose recipients live on the remote.
        timeout: Seconds the round trip may take.

    Returns:
        One of ``"waiting"``, ``"read"``, ``"delivered"`` or ``"unknown"`` per entry, as
        ``Store.local_receipt`` defines them.
    """
    if not entries:
        return []
    base, checks = _get_remote_base(), []
    for entry in entries:
        file_name, to_sid = shlex.quote(f'{_check_id(entry["id"])}.json'), shlex.quote(_check_id(entry['to_session']))
        if entry.get('route') == 'outbox':
            checks.append(f'if [ -e "$b"/outbox/{to_sid}/{file_name} ]; then printf w; else printf d; fi')
        else:
            checks.append(f'if [ -e "$b"/inbox/{to_sid}/{file_name} ]; then printf w; elif [ -e "$b"/inbox/{to_sid}/read/{file_name} ]; then printf r; else printf u; fi')
    letters = _run(master, f'b={base}; ' + '; '.join(checks), timeout=timeout).decode()
    states = {'w': 'waiting', 'r': 'read', 'd': 'delivered', 'u': 'unknown'}
    return [states.get(letter, 'unknown') for letter in letters.ljust(len(entries), 'u')]


def remote_send(master: Any, msg: dict, target: dict) -> str:
    """Place a message for a session registered on the remote, mirroring ``Store.send``.

    A target carrying ``relay_via`` runs on another machine that pulls the remote outbox, so a
    message written to its inbox would never be read.

    Args:
        master: Connection to the remote.
        msg: Message from ``new_message``.
        target: Registry entry of the recipient on the remote.

    Returns:
        ``"outbox"`` or ``"inbox"``, naming where the message was written.
    """
    box = 'outbox' if target.get('relay_via') else 'inbox'
    remote_deliver(master, msg, box)
    return box


def pull_outbox(master: Any, sid: str, store: Store, timeout: float = REMOTE_TIMEOUT_S) -> int:
    """Move the remote outbox of ``sid`` into the local inbox.

    One round trip lists and reads the message files, at most ``MAX_PULL_MESSAGES`` of them and
    ``MAX_PULL_BYTES`` in total, and the rest waits for the next pull. A single file larger than
    ``MAX_PULL_BYTES`` cannot be a valid message and is deleted unread. Each valid message is delivered locally
    with ``relayed_from`` set to the master's alias, the provenance the plugin vouches for, then
    a second round trip removes exactly the pulled names, so a file that arrived after the
    listing stays for the next pull. A message that is not addressed to ``sid`` or whose id does
    not name its file is dropped from the outbox without delivery.

    Args:
        master: Connection to the remote.
        sid: Local session id whose outbox is pulled.
        store: Local store receiving the messages.
        timeout: Seconds each round trip may take. Defaults to ``REMOTE_TIMEOUT_S``.

    Returns:
        The number of messages delivered.

    Raises:
        RemoteCommandError: A round trip exited non-zero.
        subprocess.TimeoutExpired: A round trip outlived ``timeout``.
    """
    outbox_expr = f'{_get_remote_base()}/outbox/{shlex.quote(_check_id(sid))}'
    command = (f'cd {outbox_expr} 2>/dev/null || exit 0; n=0; sz=0; '
               f'for f in *.json; do [ -f "$f" ] || continue; s=$(wc -c < "$f"); '
               f'if [ "$s" -gt {MAX_PULL_BYTES} ]; then rm -- "$f" 2>/dev/null; continue; fi; '
               f'[ "$n" -ge {MAX_PULL_MESSAGES} ] && break; [ $((sz + s)) -gt {MAX_PULL_BYTES} ] && break; '
               f'n=$((n + 1)); sz=$((sz + s)); printf "%s\\0" "$f"; cat -- "$f"; printf "\\0"; done; true')
    records = _split_nul(_run(master, command, timeout=timeout))
    done, delivered = [], 0
    for file_name_raw, content in zip(records[0::2], records[1::2]):
        file_name = file_name_raw.decode('utf-8', 'replace')
        if not MSG_FILE_RE.match(file_name):
            continue
        try:
            msg = json.loads(content)
        except ValueError:
            done.append(file_name)
            continue
        if not isinstance(msg, dict) or msg.get('to_session') != sid or f'{msg.get("id")}.json' != file_name:
            done.append(file_name)
            continue
        names = msg.pop('attachment_names', None)
        msg.pop('attachments', None)
        if names:
            try:
                msg['attachments'] = _pull_attachments(master, outbox_expr, msg['id'], names, store)
            except RemoteCommandError as error:
                if error.returncode == SSH_ERROR_EXIT:
                    raise
                continue
        done.append(file_name)
        store.deliver({**msg, 'relayed_from': master.alias})
        delivered += 1
    if done:
        dirs = [shlex.quote(f'attachments/{name[:-5]}') for name in done]
        _run(master, f'cd {outbox_expr} && rm -f -- {" ".join(shlex.quote(name) for name in done)}; rm -rf -- {" ".join(dirs)}', timeout=timeout)
    return delivered


def _pull_attachments(master: Any, outbox_expr: str, msg_id: str, names: Any, store: Store) -> list[str]:
    """Fetch the attachments of one outbox message into the local store and return their paths.

    Args:
        master: Connection to the remote.
        outbox_expr: Shell word of the recipient's remote outbox directory.
        msg_id: Id of the message.
        names: Stored file names the sender listed. Names outside ``NAME_RE`` are ignored.
        store: Local store receiving the files.

    Returns:
        The absolute local paths. Files the remote does not hold, and files beyond
        ``MAX_ATTACH_BYTES`` in total, are skipped.
    """
    wanted = [name for name in names if isinstance(name, str) and NAME_RE.match(name)][:MAX_ATTACH_FILES] if isinstance(names, list) else []
    if not wanted:
        return []
    command = (f'cd {outbox_expr}/attachments/{shlex.quote(_check_id(msg_id))} 2>/dev/null || exit 0; '
               f'for f in {" ".join(shlex.quote(name) for name in wanted)}; do [ -f "$f" ] || continue; '
               'printf "%s\\0%s\\0" "$f" "$(wc -c < "$f")"; cat -- "$f"; done')
    data, pos, total, files = _run(master, command, timeout=ATTACH_TIMEOUT_S), 0, 0, []
    while pos < len(data):
        name_end = data.index(b'\0', pos)
        size_end = data.index(b'\0', name_end + 1)
        name, size = data[pos:name_end].decode('utf-8', 'replace'), int(data[name_end + 1:size_end])
        pos = size_end + 1 + size
        total += size
        if name in wanted and total <= MAX_ATTACH_BYTES:
            files.append((name, data[size_end + 1:pos]))
    return store.attach(msg_id, files)


def remote_cleanup(master: Any, machine: str, live_sids: set[str]) -> None:
    """Remove remote session entries relayed through ``machine`` whose session is not alive.

    Entries whose ``relay_via`` differs from ``machine`` belong to other machines and are kept.

    Args:
        master: Connection to the remote.
        machine: Id of this machine from ``state.machine_id()``, matched against ``relay_via``.
        live_sids: Session ids still alive on this machine. Their entries are kept.
    """
    stale = [entry.get('session_id') for entry in remote_sessions(master)
             if entry.get('relay_via') == machine and entry.get('session_id') not in live_sids]
    names = [shlex.quote(f'{sid}.json') for sid in stale if isinstance(sid, str) and ID_RE.match(sid)]
    if names:
        _run(master, f'cd {_get_remote_base()}/sessions && rm -f -- {" ".join(names)}')
