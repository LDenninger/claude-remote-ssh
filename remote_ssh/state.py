"""Runtime directory and JSON state files shared by the plugin processes of one session.

Each Claude Code process gets a private runtime directory `runtime_base()/<key>`, keyed by
`ClaudeProc.key`. `Runtime` owns the paths inside it (owner.json, state.json, per-host control
sockets and mountpoints) and serialises read-modify-write updates with `fcntl.flock`. Both base
directories are read from the environment at call time, so tests can redirect them after import.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from remote_ssh.ident import ClaudeProc, find_claude

ALIAS_RE = r'^[a-z0-9-]{1,16}$'
DEFAULT_STATE = {'hosts': {}, 'redirect': None, 'poll_interval_s': 2}


class UnsafeDirError(Exception):
    """A private directory is a symlink, not a directory, foreign-owned or group/world-accessible."""


#---------------------------------------------------------------------
# Base directories
#---------------------------------------------------------------------

def runtime_base() -> Path:
    """Return the root of all runtime directories, `REMOTE_SSH_RUNTIME_BASE` if set."""
    return Path(os.environ.get('REMOTE_SSH_RUNTIME_BASE') or f'/tmp/remote-ssh-{os.getuid()}')


def msg_base() -> Path:
    """Return the root of the message store, `REMOTE_SSH_MSG_BASE` if set."""
    return Path(os.environ.get('REMOTE_SSH_MSG_BASE') or Path.home() / '.claude' / 'remote-ssh')


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
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as tmp_file:
            json.dump(obj, tmp_file, indent=2)
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
        """Path of state.json, the connected hosts and redirect target."""
        return self.dir / 'state.json'

    @property
    def shutdown_path(self) -> Path:
        """Path of the file whose presence asks the server to tear down and exit."""
        return self.dir / 'shutdown'

    @property
    def lock_path(self) -> Path:
        """Path of the lock file that serialises owner.json and state.json updates."""
        return self.dir / 'state.lock'

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

    def load_state(self) -> dict:
        """Return state.json merged over a copy of `DEFAULT_STATE`."""
        state = copy.deepcopy(DEFAULT_STATE)
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
        return state
