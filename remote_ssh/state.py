"""Runtime directory and JSON state files shared by the plugin processes of one session.

Each Claude Code process gets a private runtime directory `runtime_base()/<key>`, keyed by
`ClaudeProc.key`. `Runtime` owns the paths inside it (owner.json, state.json, the redirect flag,
queued context blocks, per-host control sockets and mountpoints) and serialises read-modify-write
updates with `fcntl.flock`. Both base directories are read from the environment at call time, so
tests can redirect them after import.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import secrets
import socket
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from remote_ssh.ident import ClaudeProc, find_claude

ALIAS_RE = r'^[a-z0-9-]{1,16}$'
DEFAULT_STATE = {'hosts': {}, 'redirect': None, 'poll_interval_s': 2}
#--- ssh binds `<ctl>.<16 random chars>` first: base + /<key 8>/<alias 16>/c + 17 must fit macOS's 103 ---
MAX_BASE_LEN = 58


class UnsafeDirError(Exception):
    """A private directory is a symlink, not a directory, foreign-owned or group/world-accessible."""


#---------------------------------------------------------------------
# Base directories
#---------------------------------------------------------------------

def runtime_base() -> Path:
    """Return the root of all runtime directories of this user.

    `REMOTE_SSH_RUNTIME_BASE` wins when set. Otherwise the base lives in a directory only this
    user can write, so no other user can pre-create it: `$XDG_RUNTIME_DIR/remote-ssh`, or
    `$TMPDIR/rssh` when `TMPDIR` is a macOS per-user `/var/folders/` directory. A missing or
    too long candidate falls back to `/tmp/remote-ssh-<uid>`, so control socket paths stay
    within the Unix socket limit. `scripts/gate.sh` computes the same base in shell.
    """
    override = os.environ.get('REMOTE_SSH_RUNTIME_BASE')
    if override:
        return Path(override)
    xdg = os.environ.get('XDG_RUNTIME_DIR', '')
    tmpdir = os.environ.get('TMPDIR', '')
    base = ''
    if xdg:
        base = f'{xdg}/remote-ssh'
    elif tmpdir.startswith('/var/folders/'):
        base = f'{tmpdir[:-1] if tmpdir.endswith("/") else tmpdir}/rssh'
    if not base or len(base) > MAX_BASE_LEN:
        base = f'/tmp/remote-ssh-{os.getuid()}'
    return Path(base)


def msg_base() -> Path:
    """Return the root of the message store, `REMOTE_SSH_MSG_BASE` if set."""
    return Path(os.environ.get('REMOTE_SSH_MSG_BASE') or Path.home() / '.claude' / 'remote-ssh')


def machine_id() -> str:
    """Return the id of this machine, generated once and kept in `msg_base()/machine-id.<hostname>`.

    Hostnames repeat across machines, so remote registrations name their relay machine by this id
    instead. The file name carries the hostname because the base may sit on a home directory shared
    by several nodes, each of which needs an id of its own. The id is written to a temp file and
    linked into place, so a concurrent reader sees no file or the complete id, never an empty one.
    """
    host_tag = re.sub(r'[^A-Za-z0-9._-]', '_', socket.gethostname()) or 'host'
    id_path = ensure_private_dir(msg_base()) / f'machine-id.{host_tag}'

    def read_id() -> str:
        try:
            return id_path.read_text().strip()
        except FileNotFoundError:
            return ''

    if read_id():
        return read_id()
    candidate = secrets.token_hex(8)
    fd, tmp_name = tempfile.mkstemp(dir=id_path.parent, prefix='.machine-id.')
    try:
        with os.fdopen(fd, 'w') as tmp_file:
            tmp_file.write(candidate)
        try:
            os.link(tmp_name, id_path)
        except FileExistsError:
            if not read_id():
                os.replace(tmp_name, id_path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    return read_id()


def iter_runtime_dirs() -> Iterator[Path]:
    """Yield the runtime directories under `runtime_base()`, none if the base is missing."""
    base_root = runtime_base()
    if not base_root.is_dir():
        return
    for entry_dir in sorted(base_root.iterdir()):
        if entry_dir.is_dir() and not entry_dir.is_symlink():
            yield entry_dir


#---------------------------------------------------------------------
# Validation and file primitives
#---------------------------------------------------------------------

def validate_alias(alias: str) -> str:
    """Check a host alias against `ALIAS_RE`.

    Args:
        alias: Host alias, used as a directory name, so 1 to 16 of `a-z`, `0-9` and `-`.

    Returns:
        The alias unchanged.

    Raises:
        ValueError: The alias does not match `ALIAS_RE`.
    """
    if not isinstance(alias, str) or not re.fullmatch(ALIAS_RE, alias):
        raise ValueError(f'invalid alias {alias!r}: use 1-16 characters of a-z, 0-9 and -')
    return alias


def ensure_private_dir(path: Path) -> Path:
    """Create a directory with mode 0700 if missing and verify it is private.

    Args:
        path: Directory to create. Missing parents are created with the default mode.

    Returns:
        The path unchanged.

    Raises:
        UnsafeDirError: The path is a symlink or not a directory, is owned by another user, or
            grants any group or other permission.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeDirError(f'{path} is not a real directory')
    if info.st_uid != os.getuid():
        raise UnsafeDirError(f'{path} is owned by uid {info.st_uid}, not {os.getuid()}')
    if info.st_mode & 0o077:
        raise UnsafeDirError(f'{path} has mode {stat.S_IMODE(info.st_mode):o}, expected no group or other access')
    return path


def write_json_atomic(path: Path, obj: Any) -> None:
    """Write `obj` as JSON to `path` with mode 0600, so readers see the old or the new file only."""
    write_text_atomic(path, json.dumps(obj, indent=2))


def write_text_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` with mode 0600, so readers see the old or the new file only."""
    write_bytes_atomic(path, text.encode('utf-8'))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` with mode 0600, so readers see the old or the new file only."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    """Return the parsed JSON content of `path`, or `default` if it is missing or invalid."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive `flock` on `lock_path` for the duration of the block."""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _ensure_mountpoint(path: Path) -> Path:
    """Create a mountpoint directory, tolerating an existing (possibly stale) mount."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    return path


#---------------------------------------------------------------------
# Runtime directory
#---------------------------------------------------------------------

class Runtime:
    """The private runtime directory of one Claude Code process.

    Args:
        claude: The owning Claude Code process. Creating the object creates and checks both
            `runtime_base()` and `runtime_base()/<claude.key>`.

    Attributes:
        claude: The owning Claude Code process.
        dir: The runtime directory.
    """

    def __init__(self, claude: ClaudeProc) -> None:
        self.claude = claude
        self.dir = ensure_private_dir(ensure_private_dir(runtime_base()) / claude.key)

    @classmethod
    def current(cls) -> Runtime | None:
        """Return the runtime of the Claude process owning this process, None if not found."""
        claude = find_claude()
        return cls(claude) if claude is not None else None

    #--- fixed paths ---

    @property
    def owner_path(self) -> Path:
        """Path of owner.json, the session identity written by the SessionStart hook."""
        return self.dir / 'owner.json'

    @property
    def state_path(self) -> Path:
        """Path of state.json: connected hosts, redirect target and the server's ssh options."""
        return self.dir / 'state.json'

    @property
    def shutdown_path(self) -> Path:
        """Path of the file whose presence asks the server to tear down and exit."""
        return self.dir / 'shutdown'

    @property
    def lock_path(self) -> Path:
        """Path of the lock file that serialises owner.json and state.json updates."""
        return self.dir / 'state.lock'

    @property
    def redirect_flag_path(self) -> Path:
        """Path of the file that exists while state.json names a redirect target.

        The PreToolUse hook command tests for it in shell and starts Python only when it exists.
        """
        return self.dir / 'redirect-on'

    @property
    def pending_path(self) -> Path:
        """Path of the file that exists while this session's inbox holds unclaimed messages.

        The server keeps it current on every poll. The message hook commands test for it in
        shell and start Python only when it exists.
        """
        return self.dir / 'pending'

    @property
    def need_session_path(self) -> Path:
        """Path of the file that exists while the server has no session id.

        `/reload-plugins` starts a new server without firing SessionStart, so the id never reaches
        owner.json. The message hook commands treat this flag like pending work, and the next
        UserPromptSubmit or PostToolUse hook writes the id from its payload and removes the flag.
        """
        return self.dir / 'need-session'

    @property
    def context_dir(self) -> Path:
        """Directory of queued context blocks, one `<alias>.md` per host, created if missing."""
        return ensure_private_dir(self.dir / 'context')

    #--- per-host paths ---

    def host_dir(self, alias: str) -> Path:
        """Return the private directory of a host alias, created if missing.

        Raises:
            ValueError: The alias does not match `ALIAS_RE`.
        """
        return ensure_private_dir(self.dir / validate_alias(alias))

    def ctl_path(self, alias: str) -> Path:
        """Return the ControlMaster socket path of a host alias."""
        return self.host_dir(alias) / 'c'

    def fs_path(self, alias: str) -> Path:
        """Return the project mountpoint of a host alias, created if missing."""
        return _ensure_mountpoint(self.host_dir(alias) / 'fs')

    def mem_path(self, alias: str) -> Path:
        """Return the remote memory mountpoint of a host alias, created if missing."""
        return _ensure_mountpoint(self.host_dir(alias) / 'mem')

    #--- JSON state ---

    def owner(self) -> dict:
        """Return the owner.json fields, empty if not yet written."""
        return read_json(self.owner_path, {})

    def update_owner(self, **fields: Any) -> None:
        """Merge `fields` into owner.json under the state lock."""
        with _locked(self.lock_path):
            owner = self.owner()
            owner.update(fields)
            write_json_atomic(self.owner_path, owner)

    def load_state(self, strict: bool = False) -> dict:
        """Return state.json merged over a copy of `DEFAULT_STATE`.

        Args:
            strict: Raise on a state.json that exists but is not a JSON object, instead of
                treating it as empty. Defaults to False.

        Raises:
            ValueError: With `strict`, state.json is not valid JSON or not an object.
            OSError: With `strict`, state.json exists but cannot be read.
        """
        state = copy.deepcopy(DEFAULT_STATE)
        if strict:
            try:
                stored = json.loads(self.state_path.read_text())
            except FileNotFoundError:
                stored = {}
            if not isinstance(stored, dict):
                raise ValueError('state.json is not a JSON object')
        else:
            stored = read_json(self.state_path, {})
        if isinstance(stored, dict):
            state.update(stored)
        return state

    def update_state(self, fn: Callable[[dict], None]) -> dict:
        """Apply `fn` to the state in place and save it, under the state lock.

        Args:
            fn: Mutates the loaded state dict. Its return value is ignored.

        Returns:
            The state as saved.
        """
        with _locked(self.lock_path):
            state = self.load_state()
            fn(state)
            write_json_atomic(self.state_path, state)
            if state.get('redirect'):
                self.redirect_flag_path.touch()
            else:
                self.redirect_flag_path.unlink(missing_ok=True)
        return state

    #--- queued context, handed to the session by the next UserPromptSubmit or PostToolUse hook ---

    def queue_context(self, alias: str, text: str) -> None:
        """Queue one host's context block, replacing an unclaimed older block of that host."""
        write_text_atomic(self.context_dir / f'{validate_alias(alias)}.md', text)

    def drop_context(self, alias: str) -> None:
        """Remove an unclaimed context block of a host, if any."""
        (self.dir / 'context' / f'{validate_alias(alias)}.md').unlink(missing_ok=True)

    def claim_context(self) -> list[str]:
        """Take every queued context block, sorted by alias.

        Each block is renamed before it is read, so among concurrent claimers exactly one
        gets it.

        Returns:
            The block texts, empty when nothing is queued.
        """
        queue_dir = self.dir / 'context'
        if not queue_dir.is_dir():
            return []
        blocks = []
        for block_path in sorted(queue_dir.glob('*.md')):
            claimed_path = queue_dir / f'.{block_path.name}.{os.getpid()}.claimed'
            try:
                os.rename(block_path, claimed_path)
            except FileNotFoundError:
                continue
            blocks.append(claimed_path.read_text(encoding='utf-8', errors='replace'))
            claimed_path.unlink()
        return blocks
