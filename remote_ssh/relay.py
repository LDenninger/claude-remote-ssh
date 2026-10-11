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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from remote_ssh import state

ID_RE = re.compile(r'^[A-Za-z0-9-]{1,128}$')
MSG_FILE_RE = re.compile(r'^\d+-[0-9a-f]{8}\.json$')
REMOTE_TIMEOUT_S = 30


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


def session_name(cwd: str, sid: str) -> str:
    """Return the display name ``<cwd basename>-<first 4 chars of sid>``."""
    return f'{os.path.basename(cwd.rstrip("/")) or "root"}-{sid[:4]}'


def resolve(sessions: list[dict], name_or_id: str) -> dict | None:
    """Find a session by exact id, exact name, or unique id prefix.

    Args:
        sessions: Session entries, each with ``session_id`` and ``name``.
        name_or_id: The id, the name or a prefix of the id.

    Returns:
        The matching entry, or None when nothing matches or the prefix is ambiguous.
    """
    for key in ('session_id', 'name'):
        for entry in sessions:
            if entry.get(key) == name_or_id:
                return entry
    if not name_or_id:
        return None
    matches = [entry for entry in sessions if str(entry.get('session_id', '')).startswith(name_or_id)]
    return matches[0] if len(matches) == 1 else None


def _check_id(value: str) -> str:
    """Return ``value`` if it is safe as a path component, else raise ValueError."""
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ValueError(f'unsafe id: {value!r}')
    return value


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
        """Write the session entry ``sessions/<sid>.json``, adding ``session_id``."""
        state.write_json_atomic(self.__ensure_dir('sessions') / f'{_check_id(sid)}.json', {**entry, 'session_id': sid})

    def unregister(self, sid: str) -> None:
        """Remove the session entry of ``sid`` if present."""
        (self.base / 'sessions' / f'{_check_id(sid)}.json').unlink(missing_ok=True)

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
        return state.read_json(read_dir / file_name)

    def read_messages(self, sid: str, limit: int = 50) -> list[dict]:
        """Return up to ``limit`` most recent delivered messages of ``sid``, oldest first."""
        paths = self.__list_msg_files(self.base / 'inbox' / _check_id(sid) / 'read')[-limit:] if limit > 0 else []
        msgs = [state.read_json(path) for path in paths]
        return [msg for msg in msgs if isinstance(msg, dict)]

    def outbox_files(self, sid: str) -> list[Path]:
        """Return the message files waiting in the outbox of ``sid``, sorted by id."""
        return self.__list_msg_files(self.base / 'outbox' / _check_id(sid))

    def sweep(self, live_sids: set[str], max_age_s: float = 7 * 86400) -> None:
        """Drop registrations of dead local sessions and stale message directories.

        Args:
            live_sids: Session ids known to be alive. They are never swept.
            max_age_s: Minimum age of an inbox or outbox directory of an unregistered session
                before it is deleted. Defaults to seven days.
        """
        for entry in self.sessions():
            sid = entry.get('session_id')
            if sid in live_sids or entry.get('relay_via') or not ID_RE.match(str(sid)):
                continue
            if not _is_pid_alive(entry.get('pid')):
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


def _is_pid_alive(pid: Any) -> bool:
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


def _run(master: Any, command: str, input: bytes | None = None) -> bytes:
    """Run ``command`` through ``master`` and return stdout, raising RemoteCommandError on failure."""
    result = master.run(f'umask 077; {command}', input=input, timeout=REMOTE_TIMEOUT_S)
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


def remote_unregister(master: Any, sid: str) -> None:
    """Remove ``sessions/<sid>.json`` on the remote if present."""
    _run(master, f'rm -f -- {_get_remote_base()}/sessions/{shlex.quote(_check_id(sid) + ".json")}')


def remote_sessions(master: Any) -> list[dict]:
    """Return every session entry registered on the remote, skipping unreadable files."""
    command = f'for f in {_get_remote_base()}/sessions/*.json; do [ -f "$f" ] && cat -- "$f" && printf "\\0"; done; true'
    entries = []
    for record in _split_nul(_run(master, command)):
        try:
            entry = json.loads(record)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def remote_deliver(master: Any, msg: dict) -> None:
    """Write a message atomically into the remote inbox of its recipient."""
    inbox_expr = f'{_get_remote_base()}/inbox/{shlex.quote(_check_id(msg["to_session"]))}'
    _run(master, _build_write_cmd(inbox_expr, f'{_check_id(msg["id"])}.json'), input=json.dumps(msg).encode())


def pull_outbox(master: Any, sid: str, store: Store) -> int:
    """Move the remote outbox of ``sid`` into the local inbox.

    One round trip lists and reads every message file. Each parsed message is delivered locally,
    then a second round trip removes exactly the pulled names, so a file that arrived after the
    listing stays for the next pull.

    Args:
        master: Connection to the remote.
        sid: Local session id whose outbox is pulled.
        store: Local store receiving the messages.

    Returns:
        The number of messages pulled.
    """
    outbox_expr = f'{_get_remote_base()}/outbox/{shlex.quote(_check_id(sid))}'
    command = (f'cd {outbox_expr} 2>/dev/null || exit 0; '
               f'for f in *.json; do [ -f "$f" ] || continue; printf "%s\\0" "$f"; cat -- "$f"; printf "\\0"; done; true')
    records = _split_nul(_run(master, command))
    pulled = []
    for file_name_raw, content in zip(records[0::2], records[1::2]):
        file_name = file_name_raw.decode('utf-8', 'replace')
        if not MSG_FILE_RE.match(file_name):
            continue
        try:
            msg = json.loads(content)
            store.deliver(msg)
        except (ValueError, KeyError, TypeError):
            continue
        pulled.append(file_name)
    if pulled:
        _run(master, f'cd {outbox_expr} && rm -f -- {" ".join(shlex.quote(name) for name in pulled)}')
    return len(pulled)


def remote_cleanup(master: Any, hostname: str, live_sids: set[str]) -> None:
    """Remove remote session entries relayed through ``hostname`` whose session is not alive.

    Args:
        master: Connection to the remote.
        hostname: Local hostname, matched against ``relay_via``.
        live_sids: Session ids still alive on this machine. Their entries are kept.
    """
    stale = [entry.get('session_id') for entry in remote_sessions(master)
             if entry.get('relay_via') == hostname and entry.get('session_id') not in live_sids]
    names = [shlex.quote(f'{sid}.json') for sid in stale if isinstance(sid, str) and ID_RE.match(sid)]
    if names:
        _run(master, f'cd {_get_remote_base()}/sessions && rm -f -- {" ".join(names)}')
