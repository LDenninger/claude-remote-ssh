"""MCP stdio server of the remote-ssh plugin.

The server owns every long-lived piece of state: one `HostSession` per connected alias,
holding its ControlMaster, mounts and status. `Server` implements the tools, the startup
recovery, the health and relay loop and the one-shot teardown. `serve` is the JSON-RPC 2.0
layer over newline-delimited stdin and stdout, kept separate from the tool logic so tests
can drive it with in-memory streams. `main` wires both to the real process.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable

from remote_ssh import __version__, fmt, ident, inbox, memory, mount, platform, relay, state
from remote_ssh.conn import ConnectError, Master, expand_remote_path

LOG = logging.getLogger('remote_ssh.server')

NO_RUNTIME = 'cannot locate the Claude process'
DEFAULT_PROTOCOL = '2025-06-18'
BACKOFF_MAX_S = 60.0
OWNER_WAIT_S = 10.0
WORKER_JOIN_S = 5.0
RESOLVE_TIMEOUT_S = 30.0
SSH_ERROR_EXIT = 255
POLL_PULL_TIMEOUT_S = 5.0
HEALTH_FAILURE_LIMIT = 3
TEARDOWN_MESSAGING_TIMEOUT_S = 3.0
TEARDOWN_LOCK_WAIT_S = 2.0
MEMORY_READ_TIMEOUT_S = 3.0
MEMORY_INDEX_MAX_BYTES = 100_000


#---------------------------------------------------------------------
# Configuration
#---------------------------------------------------------------------

@dataclass
class Config:
    """Server options, mapped from the plugin's user_config through `.mcp.json` env.

    Attributes:
        messaging: Register this session on connected hosts and relay messages.
        poll_interval_s: Period of the health check and message relay loop, 1 to 60 s.
        sshfs_options: Extra `-o` options appended to every mount, empty for none.
        ssh_opts: Extra ssh arguments for every master and remote command (tests only).
    """

    messaging: bool = True
    poll_interval_s: float = 2.0
    sshfs_options: str = ''
    ssh_opts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> Config:
        """Read the options from the environment, falling back to defaults on bad values."""
        def option(name: str) -> str:
            value = os.environ.get(name, '')
            return '' if value.startswith('${') else value

        try:
            poll_interval_s = min(max(float(option('REMOTE_SSH_POLL_INTERVAL') or 2), 1.0), 60.0)
        except ValueError:
            poll_interval_s = 2.0
        return cls(
            messaging=(option('REMOTE_SSH_MESSAGING') or 'true').lower() not in ('false', '0'),
            poll_interval_s=poll_interval_s,
            sshfs_options=option('REMOTE_SSH_SSHFS_OPTIONS'),
            ssh_opts=shlex.split(os.environ.get('REMOTE_SSH_SSH_OPTS', '')),
        )


#---------------------------------------------------------------------
# Tool schemas
#---------------------------------------------------------------------

MODE_OFF_WORDS = ('null', 'none', 'off')
ALIAS_SCHEMA = {'type': 'string', 'pattern': state.ALIAS_RE}

TOOLS: list[dict[str, Any]] = [
    {
        'name': 'remote_connect',
        'description': 'Connect a host over SSH, mount its project and Claude memory. Returns mounts, remote sessions and the memory index.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'alias': ALIAS_SCHEMA,
                'host': {'type': 'string', 'description': 'ssh destination: config Host or user@host.'},
                'path': {'type': 'string', 'description': 'Remote project dir, absolute or ~/.'},
            },
            'required': ['alias', 'host', 'path'],
        },
    },
    {
        'name': 'remote_disconnect',
        'description': 'Disconnect a host and release its mounts.',
        'inputSchema': {'type': 'object', 'properties': {'alias': ALIAS_SCHEMA}, 'required': ['alias']},
    },
    {
        'name': 'remote_status',
        'description': 'List hosts, their file and memory mounts (remote path to local mount), redirect target and missing local tools.',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'remote_exec',
        'description': 'Run a command on a host with bash -lc in its project dir. Returns exit code, stdout and stderr (last 30 000 chars).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'alias': ALIAS_SCHEMA,
                'command': {'type': 'string'},
                'cwd': {'type': 'string', 'description': 'Remote dir, default the project dir.'},
                'timeout_s': {'type': 'integer', 'minimum': 1, 'maximum': 3600, 'default': 600},
            },
            'required': ['alias', 'command'],
        },
    },
    {
        'name': 'remote_mode',
        'description': 'Run later Bash tool calls on a host, or locally again with null. The strings "null", "none" and "off" also mean local, any other alias must be connected.',
        'inputSchema': {
            'type': 'object',
            'properties': {'alias': {'type': ['string', 'null'], 'description': 'Connected alias or null.'}},
            'required': ['alias'],
        },
    },
    {
        'name': 'remote_sessions',
        'description': 'List Claude sessions reachable for messaging on a host, or on this machine without alias.',
        'inputSchema': {'type': 'object', 'properties': {'alias': ALIAS_SCHEMA}},
    },
    {
        'name': 'remote_send',
        'description': 'Message a Claude session on a host, or on this machine without alias.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'alias': ALIAS_SCHEMA,
                'session': {'type': 'string', 'description': 'Session name or id prefix.'},
                'body': {'type': 'string'},
                'files': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Local files to attach, at most 10 and 5 MB in total.'},
            },
            'required': ['session', 'body'],
        },
    },
    {
        'name': 'remote_inbox',
        'description': 'Show new and recent messages for this session, and whether the messages it sent were read or answered.',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
]
TOOL_NAMES = {tool['name'] for tool in TOOLS}


#---------------------------------------------------------------------
# Helpers
#---------------------------------------------------------------------

def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with seconds precision."""
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def build_resolve_script(path: str) -> str:
    """Return a POSIX sh script printing the remote home and the physical project path.

    `cd` followed by `pwd -P` works on GNU and BSD alike, unlike `realpath -m`. The script
    prints `home=<dir>`, then `path=<dir>` when the directory exists or `missing=1` when
    it does not. Inside a git repository it also prints `root=<dir>`, the main worktree's
    root, which names the project's Claude memory directory.

    Args:
        path: Requested remote path, absolute, `~`-relative or relative to the home.
    """
    return (f'printf "home=%s\\n" "$HOME" && cd ~ && '
            f'if cd -- {expand_remote_path(path)} 2>/dev/null; then printf "path=%s\\n" "$(pwd -P)"; '
            'if c=$(git rev-parse --git-common-dir 2>/dev/null) && [ "${c##*/}" = .git ]; then printf "root=%s\\n" "$(cd -- "$c/.." && pwd -P)"; fi; '
            'else printf "missing=1\\n"; fi')


def cleanup_runtime_dir(run_dir: Path, ssh_opts: list[str]) -> bool:
    """Unmount, stop and delete a runtime directory left by a dead Claude process.

    Deletion never descends below the second level: files there are unlinked and directories,
    the mountpoints among them, are removed with `rmdir`, which fails on a mounted or non-empty
    directory. A delete therefore never reaches remote files, even through a mount the check
    missed.

    Args:
        run_dir: Runtime directory of the dead owner.
        ssh_opts: Extra ssh arguments for the `-O exit` call.

    Returns:
        True when the directory was removed. False when something survived, a mount or an
        unexpected file, in which case the rest is kept.
    """
    hosts = state.read_json(run_dir / 'state.json', {}).get('hosts', {})
    alias_dirs = [child for child in run_dir.iterdir() if child.is_dir() and not child.is_symlink()]
    for alias_dir in alias_dirs:
        for sub in ('mem', 'fs'):
            mount.unmount(alias_dir / sub)
        entry = hosts.get(alias_dir.name)
        if entry and entry.get('host'):
            Master(alias_dir.name, entry['host'], alias_dir / 'c', ssh_opts).stop()
    for child in list(run_dir.iterdir()):
        if child.is_dir() and not child.is_symlink():
            for grandchild in list(child.iterdir()):
                remove_entry(grandchild, rmdir=grandchild.is_dir() and not grandchild.is_symlink())
            remove_entry(child, rmdir=True)
        else:
            remove_entry(child, rmdir=False)
    if not remove_entry(run_dir, rmdir=True):
        LOG.warning('keeping %s: some entries could not be removed', run_dir)
        return False
    return True


def remove_entry(path: Path, rmdir: bool) -> bool:
    """Remove one file with `unlink` or one empty directory with `rmdir`, logging a failure.

    Returns:
        True when the entry is gone afterwards.
    """
    try:
        if rmdir:
            os.rmdir(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        LOG.warning('cannot remove %s: %s', path, exc)
        return False
    return True


#---------------------------------------------------------------------
# Host session
#---------------------------------------------------------------------

class HostSession:
    """One connected remote host: its ControlMaster, mounts and status.

    Lifecycle methods (`connect`, `reconnect`, `disconnect`) run under `lock`, which the
    server also uses to skip a host whose lifecycle step is still running.

    Args:
        alias: Validated host alias, also the runtime subdirectory name.
        host: ssh destination.
        path: Requested remote path, resolved to `abs_path` on connect.
        runtime: Runtime directory of the owning Claude process.
        config: Server options.
        tools: Local tool availability from `platform.detect()`.

    Attributes:
        status: "connecting", "connected", "reconnecting" or "failed".
        abs_path: Resolved remote project path, None before connect.
        mem_uri: Remote memory directory, None before connect.
        mounted_fs: The project path is mounted at `fs_dir`.
        mounted_mem: The remote memory directory is mounted at `mem_dir`.
        error: Last error or warning text, None when there is none.
    """

    def __init__(self, alias: str, host: str, path: str, runtime: state.Runtime, config: Config, tools: dict[str, bool]) -> None:
        self.alias = alias
        self.host = host
        self.path = path
        self.config = config
        self.tools = tools
        self.master = Master(alias, host, runtime.ctl_path(alias), config.ssh_opts)
        self.fs_dir = runtime.fs_path(alias)
        self.mem_dir = runtime.mem_path(alias)
        self.runtime_dir = runtime.dir
        self.status = 'connecting'
        self.abs_path: str | None = None
        self.slug: str | None = None
        self.mem_uri: str | None = None
        self.mem_found = False
        self.mounted_fs = False
        self.mounted_mem = False
        self.error: str | None = None
        self.retry_at = 0.0
        self.retry_delay = 1.0
        self.failures = 0
        self.lock = threading.Lock()

    #---------------------------------------------------------------------
    # Lifecycle
    #---------------------------------------------------------------------

    def connect(self) -> list[str]:
        """Start the master, resolve the remote path and mount what is available.

        Returns:
            Warning lines for features that were skipped, empty when everything mounted.

        Raises:
            ConnectError: The master did not come up or the path did not resolve. Partial
                state is released before raising.
        """
        self.master.start()
        try:
            self.__resolve_paths()
            warnings = self.__mount_all()
        except BaseException:
            self.release()
            raise
        self.status = 'connected'
        self.error = '; '.join(warnings) or None
        return warnings

    def reconnect(self) -> None:
        """Restart a dropped master and remount whatever was mounted before.

        Raises:
            ConnectError: The master did not come back.
        """
        self.master.stop()
        self.master.start()
        for was_mounted, mount_dir, remote_uri in ((self.mounted_fs, self.fs_dir, self.abs_path), (self.mounted_mem, self.mem_dir, self.mem_uri)):
            if not was_mounted or remote_uri is None or mount.is_mounted(mount_dir):
                continue
            mount.unmount(mount_dir)
            try:
                mount.mount(self.master, remote_uri, mount_dir, self.config.sshfs_options)
            except mount.MountError as exc:
                LOG.warning('%s: remount of %s failed: %s', self.alias, mount_dir, exc)
        self.status = 'connected'
        self.error = None

    def disconnect(self, store: relay.Store | None, session_id: str | None) -> list[tuple[str, str]]:
        """Unmount, pull pending replies, unregister this session remotely, stop the master.

        The local steps come first and are bounded. The messaging steps need the remote, so
        they run only while the master's socket is listening and give up after
        `TEARDOWN_MESSAGING_TIMEOUT_S` each, so a frozen host never stalls teardown.

        Args:
            store: Local message store, None when messaging is off.
            session_id: Own session id, None when unknown.

        Returns:
            `(key, value)` report fields, one per step that did something or failed.
        """
        report = self.unmount_all()
        if store is not None and session_id and self.status == 'connected' and self.master.is_listening():
            try:
                pulled = relay.pull_outbox(self.master, session_id, store, timeout=TEARDOWN_MESSAGING_TIMEOUT_S)
                if pulled:
                    report.append(('replies fetched', str(pulled)))
                relay.remote_unregister(self.master, session_id, timeout=TEARDOWN_MESSAGING_TIMEOUT_S)
            except Exception as exc:
                LOG.warning('%s: messaging teardown failed: %r', self.alias, exc)
                report.append(('teardown_failed', relay.clean_text(repr(exc), 200)))
        report.extend(self.release())
        return report

    def unmount_all(self) -> list[tuple[str, str]]:
        """Unmount files and memory. Never raises.

        Returns:
            `unmounted` and `unmount_failed` fields naming the mounts, each only when it applies.
        """
        done: dict[bool, list[str]] = {True: [], False: []}
        for name, mount_dir in (('files', self.fs_dir), ('memory', self.mem_dir)):
            if mount.is_mounted(mount_dir):
                done[mount.unmount(mount_dir)].append(name)
        self.mounted_fs = self.mounted_mem = False
        return [(key, ', '.join(names)) for key, names in (('unmounted', done[True]), ('unmount_failed', done[False])) if names]

    def release(self) -> list[tuple[str, str]]:
        """Unmount files and memory, then stop the master. Never raises.

        Returns:
            The `unmount_all` fields, plus `master_failed` when the master did not stop.
        """
        report = self.unmount_all()
        try:
            self.master.stop()
        except Exception as exc:
            LOG.exception('%s: master stop failed', self.alias)
            report.append(('master_failed', relay.clean_text(exc, 200)))
        return report

    def is_healthy(self) -> bool:
        """Return whether the ControlMaster process runs and its control socket accepts connections.

        A local probe without an ssh process, so a master that is busy or slow to answer
        `-O check` is not reported as down.
        """
        return not self.master.exited() and self.master.is_listening()

    #---------------------------------------------------------------------
    # Views
    #---------------------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        """Return the `state.json` host entry."""
        return {
            'host': self.host,
            'path': self.abs_path or self.path,
            'slug': self.slug,
            'status': self.status,
            'mounted_fs': self.mounted_fs,
            'mounted_mem': self.mounted_mem,
            'error': self.error,
        }

    def memory_block(self) -> str:
        """Return the remote memory block: the index read over ssh, its links left relative.

        The index is read through the ControlMaster with a `MEMORY_READ_TIMEOUT_S` limit, never
        through the sshfs mount, whose reads block while the host hangs. The index is also
        saved to `memory.cache_path` for the SessionStart hook. A host that does not answer
        in time, or whose index is missing or blank, yields an empty string.
        """
        if not self.mounted_mem or self.mem_uri is None:
            return ''
        index_path = shlex.quote(f'{self.mem_uri}/MEMORY.md')
        try:
            result = self.master.run(f'head -c {MEMORY_INDEX_MAX_BYTES} -- {index_path}', timeout=MEMORY_READ_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            LOG.warning('%s: reading MEMORY.md timed out after %.0f s', self.alias, MEMORY_READ_TIMEOUT_S)
            return ''
        if result.returncode != 0:
            return ''
        index_md = result.stdout.decode('utf-8', errors='replace')
        if not index_md.strip():
            memory.cache_path(self.runtime_dir, self.alias).unlink(missing_ok=True)
            return ''
        try:
            cache = memory.cache_path(self.runtime_dir, self.alias)
            state.ensure_private_dir(cache.parent)
            state.write_text_atomic(cache, index_md)
        except OSError:
            LOG.exception('%s: caching MEMORY.md failed', self.alias)
        return memory.render_block(self.alias, index_md)

    #---------------------------------------------------------------------
    # Private helpers
    #---------------------------------------------------------------------

    def __resolve_paths(self) -> None:
        """Resolve the remote home, the absolute project path, its slug and memory directory."""
        result = self.master.run(build_resolve_script(self.path), timeout=RESOLVE_TIMEOUT_S)
        lines = result.stdout.decode('utf-8', errors='replace').splitlines()
        values = {line.split('=', 1)[0]: line.split('=', 1)[1] for line in lines if line.startswith(('home=', 'path=', 'root=', 'missing='))}
        if values.get('missing'):
            raise ConnectError(f'remote directory {self.path!r} does not exist on {self.host} or is not accessible')
        if result.returncode != 0 or not values.get('home') or not values.get('path'):
            raise ConnectError(f'cannot resolve remote path {self.path!r}: {result.stderr.decode("utf-8", errors="replace").strip()}')
        self.abs_path = values['path']
        project_root = values.get('root') or self.abs_path
        self.slug = memory.slug(project_root)
        self.mem_uri = memory.remote_memory_dir(values['home'], project_root)
        self.mem_found = self.master.run(f'test -d {shlex.quote(self.mem_uri)}', timeout=RESOLVE_TIMEOUT_S).returncode == 0

    def __mount_all(self) -> list[str]:
        """Mount the project path and, when it exists, the memory directory.

        Returns:
            Warning lines for skipped or failed mounts.
        """
        if not (self.tools.get('sshfs') and self.tools.get('fuse')):
            return ['mounts skipped: sshfs or FUSE missing locally (remote_status lists install commands)']
        warnings = []
        try:
            mount.mount(self.master, self.abs_path, self.fs_dir, self.config.sshfs_options)
            self.mounted_fs = True
        except mount.MountError as exc:
            warnings.append(f'file mount failed: {exc}')
        if not self.mem_found:
            warnings.append(f'no remote memory directory at {self.mem_uri}, memory mount skipped')
            return warnings
        try:
            mount.mount(self.master, self.mem_uri, self.mem_dir, self.config.sshfs_options)
            self.mounted_mem = True
        except mount.MountError as exc:
            warnings.append(f'memory mount failed: {exc}')
        return warnings


#---------------------------------------------------------------------
# Server: tools and lifecycle
#---------------------------------------------------------------------

class Server:
    """Tool implementations, startup recovery, health loop and teardown.

    Args:
        runtime: Runtime directory of the owning Claude process, None when it was not
            found. Every tool call then fails with `runtime_error`.
        config: Server options.
        store: Local message store. Defaults to `relay.Store()` at the configured base.
        on_exit: Called after a teardown that ends the process. Defaults to `os._exit(0)`.
        runtime_error: Why `runtime` is None, shown in tool errors. Defaults to `NO_RUNTIME`.

    Attributes:
        hosts: Live host sessions by alias.
        registered_sid: Session id registered for messaging, locally and on every connected
            host. None before the first registration or with messaging off.
    """

    def __init__(self, runtime: state.Runtime | None, config: Config, store: relay.Store | None = None,
                 on_exit: Callable[[], None] | None = None, runtime_error: str = NO_RUNTIME) -> None:
        self.runtime = runtime
        self.runtime_error = runtime_error
        self.config = config
        self.store = store if store is not None else relay.Store()
        self.on_exit = on_exit or (lambda: os._exit(0))
        self.hosts: dict[str, HostSession] = {}
        self.tools = platform.detect()
        self.hostname = socket.gethostname()
        self.started = now_iso()
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._teardown_lock = threading.Lock()
        self._torn_down = False
        self._sid_lock = threading.Lock()
        self.registered_sid: str | None = None
        self._autoconnect_sid: str | None = None
        self._startup_done = False
        self._autoconnect_lock = threading.Lock()

    #---------------------------------------------------------------------
    # Tool dispatch
    #---------------------------------------------------------------------

    def call_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool and return its text result.

        Args:
            name: Tool name from `TOOLS`.
            args: Tool arguments as sent by the client.

        Returns:
            The result text in the grammar of `fmt`, whose first line is `<tool> <host> · <facts>`,
            and whether it is an error.
        """
        word, alias = name.removeprefix('remote_'), args.get('alias')
        host = alias if isinstance(alias, str) else None
        if self.runtime is None:
            return fmt.error(word, host, self.runtime_error, 'restart Claude Code, then retry'), True
        schema = next(tool['inputSchema'] for tool in TOOLS if tool['name'] == name)
        missing_args = [key for key in schema.get('required', []) if key not in args]
        if missing_args:
            return fmt.error(word, host, f'missing argument(s) {", ".join(missing_args)}', f'call {name} again with {", ".join(missing_args)}'), True
        try:
            return getattr(self, f'tool_{name}')(args)
        except Exception as exc:
            LOG.exception('tool %s failed', name)
            return fmt.error(word, host, relay.clean_text(exc, 300), f'check the arguments and retry; details are in {self.runtime.dir / "server.log"}'), True

    def tool_remote_connect(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Connect `args.alias` to `args.host` at `args.path`."""
        return self.connect_host(args['alias'], args['host'], args['path'])

    def tool_remote_disconnect(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Disconnect `args.alias`."""
        return self.disconnect_host(args['alias'])

    def tool_remote_status(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Report every host in a table, the redirect target, messaging and missing local tools."""
        saved = self.runtime.load_state()
        with self.lock:
            sessions = dict(self.hosts)
        rows, notes, mounts = [], [], []
        for alias, session in sorted(sessions.items()):
            state_word = 'master-down' if session.status == 'connected' and not session.is_healthy() else session.status
            rows.append([alias, state_word, 'mounted' if session.mounted_fs else '-', 'mounted' if session.mounted_mem else '-', self.__session_count(session)])
            lines = [f'files: {session.abs_path} -> {session.fs_dir}' if session.mounted_fs else None,
                     f'memory: {session.mem_uri} -> {session.mem_dir}' if session.mounted_mem else None]
            mounts.append(fmt.block(f'mounts of {alias}', '\n'.join(line for line in lines if line)))
            if session.error:
                notes.append(fmt.block(f'error from {alias} (data)', fmt.quote(relay.clean_text(session.error, 300))))
        for alias, entry in sorted(saved['hosts'].items()):
            if alias not in sessions:
                rows.append([alias, str(entry.get('status')), '-', '-', '-'])
                if entry.get('error'):
                    notes.append(fmt.block(f'error from {alias} (data)', fmt.quote(relay.clean_text(entry['error'], 300))))
        redirect = saved.get('redirect')
        return fmt.compose(
            fmt.head('status', None, fmt.count(len(rows), 'host'), f'redirect {redirect or "off"}'),
            fmt.table(['host', 'state', 'files', 'memory', 'sessions'], rows) if rows else '',
            *fmt.fields(('messaging', 'on' if self.config.messaging else 'off'), ('session', self.session_id() or 'unknown'), ('runtime', self.runtime.dir),
                        ('next', None if rows else 'remote_connect("<alias>") with the ssh host and project path')),
            *mounts,
            *notes,
            fmt.block('missing tools', platform.missing_message(platform.missing(self.tools))),
        ), False

    def tool_remote_exec(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Run `args.command` on `args.alias`.

        `timeout_s` must be an integer in 1..3600. The output is returned for every exit code, and
        only an exit of 124 or more (timeout, signal, ssh failure 255) sets the error flag, since
        a plain non-zero exit such as an empty `grep` is a normal result. Exit 255 is rendered as
        an error with `reason:` and `next:`.
        """
        alias = args['alias']
        timeout_s = 600 if args.get('timeout_s') is None else args['timeout_s']
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or not 1 <= timeout_s <= 3600:
            return fmt.error('exec', alias, f'timeout_s must be an integer from 1 to 3600, got {timeout_s!r}', 'pass timeout_s between 1 and 3600'), True
        session, error = self.__connected('exec', alias)
        if session is None:
            return error, True
        stdout, stderr, code = session.master.exec(args['command'], args.get('cwd') or session.abs_path, timeout=timeout_s)
        blocks = [fmt.block(name, text) for name, text in (('stdout', stdout), ('stderr', stderr))]
        if code == SSH_ERROR_EXIT:
            return fmt.compose(fmt.error('exec', alias, 'ssh failed (exit 255)', 'call remote_status; once the host is connected again, retry'), *blocks), True
        return fmt.compose(fmt.head('exec', alias, f'exit {code}'), *blocks), code >= 124

    def tool_remote_mode(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Set or clear the Bash redirect target.

        None, an empty alias, and the strings "null", "none" and "off" (unless such an alias is
        connected) clear it. Any other alias must be connected.
        """
        alias = args.get('alias')
        if isinstance(alias, str) and alias.lower() in MODE_OFF_WORDS and alias not in self.hosts:
            alias = None
        if alias:
            session, error = self.__connected('mode', alias)
            if session is None:
                return error, True
        self.runtime.update_state(lambda saved: saved.update(redirect=alias or None))
        if alias:
            return fmt.compose(fmt.head('mode', alias, 'bash redirected'), 'local: prefix a command with local: to run it here'), False
        return fmt.head('mode', None, 'bash local'), False

    def tool_remote_sessions(self, args: dict[str, Any]) -> tuple[str, bool]:
        """List sessions registered on `args.alias`, or locally without alias."""
        alias = args.get('alias')
        if alias:
            session, error = self.__connected('sessions', alias)
            if session is None:
                return error, True
            sessions = relay.remote_sessions(session.master)
        else:
            sessions = self.store.sessions()
        own = self.session_id()
        rows = []
        for entry in sessions:
            name, cwd = relay.clean_text(entry.get('name')), relay.clean_text(entry.get('cwd'))
            if entry.get('session_id') == own:
                #--- on a host, the own entry is a relay registration of a session that runs here ---
                name, cwd = f'{name} (this session)', f'{self.hostname}:{cwd}' if alias else cwd
                status = f'runs on {self.hostname}' if alias else 'this session'
            else:
                status = f'via {relay.clean_text(entry["relay_via"])}' if entry.get('relay_via') else '-'
            rows.append([name, relay.clean_text(entry.get('session_id'), 8), cwd, status])
        return fmt.compose(fmt.head('sessions', alias, len(sessions)), fmt.table(['name', 'id', 'cwd', 'status'], rows) if rows else ''), False

    def tool_remote_send(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Send `args.body` and the optional `args.files` to `args.session`, on `args.alias` or locally."""
        alias = args.get('alias')
        own = self.session_id()
        if not own:
            return fmt.error('send', alias, 'own session id unknown (SessionStart hook has not run)', 'submit another prompt, then retry'), True
        if not self.config.messaging:
            return fmt.error('send', alias, 'messaging is disabled in the plugin options', 'ask the user to enable messaging in /config'), True
        session = None
        if alias:
            session, error = self.__connected('send', alias)
            if session is None:
                return error, True
            sessions = relay.remote_sessions(session.master)
        else:
            sessions = self.store.sessions()
        target = relay.resolve(sessions, args['session'])
        if target is None:
            names = ', '.join(sorted({relay.clean_text(entry.get('name')) for entry in sessions})) or 'none'
            return fmt.error('send', alias, f'no session {relay.clean_text(args["session"])!r} on {alias or "this machine"}: it may have ended, '
                                            'been resumed under a new id, or the name may be wrong',
                             f'pick one of the listed names, or call remote_sessions{f"({alias!r})" if alias else ""} again',
                             ('sessions', names)), True
        body_chars = len(args['body'])
        if body_chars > relay.MAX_BODY_CHARS:
            return fmt.error('send', alias, f'body is {body_chars} characters, the limit is {relay.MAX_BODY_CHARS}',
                             'shorten the body or attach it as a file'), True
        owner = self.runtime.owner()
        try:
            files = relay.check_attachments(args['files'], owner.get('cwd', '')) if args.get('files') else []
        except ValueError as exc:
            return fmt.error('send', alias, relay.clean_text(exc, 300), f'attach existing regular files, at most {relay.MAX_ATTACH_FILES} and {relay.MAX_ATTACH_BYTES // 2 ** 20} MB in total'), True
        msg = relay.new_message(own, owner.get('name', ''), self.hostname, target['session_id'], args['body'])
        queued = bool(target.get('relay_via'))
        contents = [(name, source.read_bytes()) for source, name in files]
        if files:
            if session is not None:
                paths = relay.remote_attach(session.master, msg['id'], files, target['session_id'] if queued else None)
            else:
                paths = self.store.attach(msg['id'], contents, f'outbox/{target["session_id"]}/attachments' if queued else 'attachments')
            #--- the machine that pulls from an outbox lists its own paths, so the message carries the names ---
            msg.update({'attachment_names': [name for _, name in files]} if queued else {'attachments': paths})
        route = relay.remote_send(session.master, msg, target) if session is not None else self.store.send(msg)
        name = relay.clean_text(target.get('name'))
        try:
            self.store.log_sent(msg, name, alias, route)
        except Exception:
            LOG.exception('logging the sent message failed')
        sizes = sum(len(content) for _, content in contents)
        return fmt.compose(
            fmt.head('send', alias, name, 'queued for pickup' if queued else 'delivered'),
            *fmt.fields(('attachments', f'{fmt.count(len(files), "file")}, {sizes // 1024 + 1} KB' if files else None),
                        ('reply', "arrives in this conversation once the recipient's user lets it answer"))), False

    def tool_remote_inbox(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Claim and show new messages, list recently delivered ones and the state of the sent ones."""
        own = self.session_id()
        if not own:
            return fmt.error('inbox', None, 'own session id unknown (SessionStart hook has not run)', 'submit another prompt, then retry'), True
        delivered = self.store.read_messages(own)
        new_msgs = inbox.claim_pending(self.store, own, limit=None)
        sent = self.__sent_rows(own)
        return fmt.compose(
            fmt.head('inbox', None, f'{len(new_msgs)} new', f'{len(delivered)} delivered', f'{len(sent)} sent' if sent else None),
            fmt.block('new', '\n'.join(inbox.format_row(msg) for msg in new_msgs)),
            fmt.block('delivered', '\n'.join(inbox.format_row(msg) for msg in delivered)),
            fmt.block('sent', fmt.table(['to', 'sent', 'state', 'message'], sent) if sent else ''),
        ), False

    #---------------------------------------------------------------------
    # Connect and disconnect
    #---------------------------------------------------------------------

    def connect_host(self, alias: str, host: str, path: str, background: bool = False) -> tuple[str, bool]:
        """Connect one host and record it in `state.json`.

        Args:
            alias: Host alias.
            host: ssh destination.
            path: Remote project path.
            background: The connect ran from startup recovery or autoconnect, which the report notes.

        Returns:
            The `remote_connect` result text and whether it is an error.
        """
        try:
            alias = state.validate_alias(alias)
        except ValueError as exc:
            return fmt.error('connect', None, str(exc), 'use an alias of letters, digits, - and _'), True
        if not host or host.startswith('-'):
            return fmt.error('connect', alias, f'invalid host {host!r}: it must be non-empty and must not start with -', 'pass the ssh config Host or user@host'), True
        with self.lock:
            existing = self.hosts.get(alias)
            if existing is not None:
                return fmt.error('connect', alias, f'already {existing.status} ({existing.host})', 'call remote_disconnect first, or use the existing connection'), True
            session = HostSession(alias, host, path, self.runtime, self.config, self.tools)
            self.hosts[alias] = session
        with session.lock:
            try:
                warnings = session.connect()
            except Exception as exc:
                LOG.warning('connect %s failed: %s', alias, exc)
                with self.lock:
                    self.hosts.pop(alias, None)
                session.status, session.error = 'failed', str(exc)
                self.__save_host(session)
                reason, _, detail = str(exc).strip().partition('\n')
                return fmt.compose(fmt.error('connect', alias, relay.clean_text(reason, 200), 'check the ssh host, the path and the credentials with the user, then call remote_connect again'),
                                   fmt.block(f'detail from {alias} (data)', fmt.quote(detail.strip()) if detail.strip() else '')), True
            warnings.extend(self.__register_remote(session, self.messaging_session_id()))
            self.__save_host(session)
        return self.__connect_report(session, warnings, background), False

    def disconnect_host(self, alias: str) -> tuple[str, bool]:
        """Disconnect one host and drop it from `state.json`.

        Returns:
            The teardown report and whether it is an error.
        """
        with self.lock:
            session = self.hosts.pop(alias, None)

        def drop(saved: dict) -> None:
            saved['hosts'].pop(alias, None)
            if saved.get('redirect') == alias:
                saved['redirect'] = None

        if session is None:
            known = alias in self.runtime.load_state()['hosts']
            self.runtime.update_state(drop)
            if known:
                return fmt.compose(fmt.head('disconnect', alias, 'was not connected'), 'removed: saved state'), False
            return self.__not_connected('disconnect', alias), True
        had_memory = session.mounted_mem
        with session.lock:
            report = session.disconnect(self.__messaging_store(), self.messaging_session_id())
        self.runtime.update_state(drop)
        self.runtime.drop_context(alias)
        memory.cache_path(self.runtime.dir, alias).unlink(missing_ok=True)
        note = f'memory paths under {session.mem_dir} are gone; reconnect {alias} to read its memory again' if had_memory else None
        return fmt.compose(fmt.head('disconnect', alias), *fmt.fields(*report, ('note', note))), False

    #---------------------------------------------------------------------
    # Startup, loop and teardown
    #---------------------------------------------------------------------

    def start(self) -> None:
        """Start the lifecycle loop and the startup recovery, each in a daemon thread."""
        threading.Thread(target=self.run_loop, name='lifecycle', daemon=True).start()
        threading.Thread(target=self.run_startup, name='startup', daemon=True).start()

    def run_startup(self) -> None:
        """Sweep stale runtime dirs, recover the previous instance's hosts and autoconnect."""
        try:
            self.runtime.update_owner(server_pid=os.getpid())
            self.runtime.update_state(lambda saved: saved.update(poll_interval_s=self.config.poll_interval_s, messaging=self.config.messaging, ssh_opts=self.config.ssh_opts))
            self._autoconnect_sid = self.wait_session_id(OWNER_WAIT_S)
            self.sync_session_id()
            self.sweep()
            targets = self.__recover_previous()
            targets.extend(self.__autoconnect_targets())
            self.runtime.update_owner(autoconnect_read=True)
        except Exception:
            LOG.exception('startup failed')
            return
        self.__connect_all(targets)
        self._startup_done = True

    def autoconnect_new_session(self) -> None:
        """Connect the autoconnect hosts of a session that began after startup.

        `/clear` and `/resume` keep the process and the server but make SessionStart write a
        new session id and a fresh `autoconnect.json`. Hosts of that file that are not
        connected yet are connected in the background. Does nothing before startup finished,
        while the id is unknown or unchanged.
        """
        session_id = self.session_id()
        with self._autoconnect_lock:
            if not self._startup_done or not session_id or session_id == self._autoconnect_sid:
                return
            self._autoconnect_sid = session_id
        self.__connect_all(self.__autoconnect_targets())

    def run_loop(self) -> None:
        """Tick every `poll_interval_s` until teardown."""
        while not self._stop.wait(self.config.poll_interval_s):
            try:
                self.tick()
            except Exception:
                LOG.exception('loop tick failed')

    def tick(self) -> None:
        """Handle the shutdown file and a dead owner, then check and relay every host.

        SessionEnd ends the session's connections but not the process, which may go on with
        another session, so the server keeps running. Only the owner's exit, EOF or a signal
        ends the process.
        """
        if self.runtime.shutdown_path.exists():
            LOG.info('SessionEnd: disconnecting every host')
            self.end_session()
            self.runtime.shutdown_path.unlink(missing_ok=True)
        if not ident.alive(self.runtime.claude):
            LOG.info('owner Claude process is gone')
            self.teardown_all()
            self.on_exit()
            return
        self.sync_session_id()
        self.autoconnect_new_session()
        self.update_pending_flag()
        with self.lock:
            sessions = list(self.hosts.values())
        store = self.__messaging_store()
        session_id = self.messaging_session_id()
        for session in sessions:
            if not session.lock.acquire(blocking=False):
                continue
            threading.Thread(target=self.__tick_host, args=(session, store, session_id), name=f'tick-{session.alias}', daemon=True).start()

    def teardown_all(self) -> None:
        """Disconnect every host and unregister this session, exactly once, before exit.

        Hosts stay listed in `state.json` with status "reconnecting" so a restarted server
        reconnects them, and the redirect target and its flag stay, so a redirected Bash call
        is denied rather than run locally until then. Concurrent callers wait for the first
        teardown to finish.
        """
        with self._teardown_lock:
            if self._torn_down:
                return
            self._stop.set()
            self.__disconnect_all(keep_in_state=True)
            if self.runtime is not None:
                self.runtime.pending_path.unlink(missing_ok=True)
                self.runtime.need_session_path.unlink(missing_ok=True)
            self._torn_down = True

    def end_session(self) -> None:
        """Disconnect every host and unregister this session, keeping the server running.

        Hosts and the redirect target leave `state.json`: the session that used them ended,
        so nothing reconnects them. A later session id registers anew.
        """
        aliases = list(self.runtime.load_state()['hosts'])
        self.__disconnect_all(keep_in_state=False)
        self.runtime.update_state(lambda saved: saved.update(hosts={}, redirect=None))
        for alias in aliases:
            self.runtime.drop_context(alias)
            memory.cache_path(self.runtime.dir, alias).unlink(missing_ok=True)

    def update_pending_flag(self) -> None:
        """Keep the pending and need-session flags current for the hook commands.

        The pending flag exists while this session's inbox holds messages. The need-session flag
        exists while owner.json holds no session id, which asks the next message hook to write it.
        """
        try:
            session_id = self.session_id()
            if session_id:
                self.runtime.need_session_path.unlink(missing_ok=True)
            else:
                self.runtime.need_session_path.touch()
            if session_id and self.store.has_pending(session_id):
                self.runtime.pending_path.touch()
            else:
                self.runtime.pending_path.unlink(missing_ok=True)
        except Exception:
            LOG.exception('pending flag update failed')

    def shutdown(self) -> None:
        """Tear down and exit the process; the signal and EOF path."""
        self.teardown_all()
        self.on_exit()

    def sweep(self) -> None:
        """Clean runtime dirs of dead Claude processes and stale message dirs."""
        live_sids = set()
        for run_dir in state.iter_runtime_dirs():
            owner = state.read_json(run_dir / 'owner.json', {})
            if run_dir == self.runtime.dir:
                live_sids.add(owner.get('session_id'))
                continue
            pid, start = owner.get('claude_pid'), owner.get('claude_proc_start')
            if pid and start:
                dead = not ident.alive(ident.ClaudeProc(int(pid), str(start)))
            else:
                #--- a session that only ran /reload-plugins has no Claude pid: its server decides ---
                server_pid = owner.get('server_pid')
                dead = bool(server_pid) and not relay.is_pid_alive(server_pid)
                pid = pid or server_pid
            if not dead:
                live_sids.add(owner.get('session_id'))
                continue
            LOG.info('sweeping %s (owner %s dead)', run_dir, pid)
            cleanup_runtime_dir(run_dir, self.config.ssh_opts)
        live_sids.discard(None)
        self.store.sweep(live_sids)

    def session_id(self) -> str | None:
        """Return the session id the SessionStart hook wrote into owner.json, None before that."""
        return self.runtime.owner().get('session_id') if self.runtime is not None else None

    def messaging_session_id(self) -> str | None:
        """Return the session id registered for messaging, else the one in owner.json."""
        return self.registered_sid or self.session_id()

    def sync_session_id(self) -> None:
        """Register the session id from owner.json, replacing an older registration.

        `/clear` keeps the Claude process and the server but makes SessionStart write a new session
        id, and `--resume` starts a new process with a new id. The new registration takes over the
        name of the session that ended in the same directory, and keeps the ids it replaced in
        `previous_ids`, so messages for an old id or name reach the new session. The old id is
        unregistered locally and on every connected host. Messages still unread under an old id,
        local ones and those waiting in a host's outbox, move to the new id's inbox. Does nothing
        with messaging off or while the id is unknown or unchanged.
        """
        if not self.config.messaging:
            return
        with self._sid_lock:
            session_id, old_sid = self.session_id(), self.registered_sid
            if not session_id or session_id == old_sid:
                return
            if old_sid:
                LOG.info('session id changed from %s to %s, re-registering', old_sid, session_id)
                self.store.unregister(old_sid)
            cwd = self.runtime.owner().get('cwd', '')
            taken = self.store.take_over(cwd, session_id) if cwd else None
            previous = list(dict.fromkeys([*(taken['previous_ids'] if taken else []), *([old_sid] if old_sid else [])]))
            self.runtime.update_owner(name=taken['name'] if taken and taken.get('name') else relay.session_name(cwd, session_id), previous_ids=previous)
            self.store.register(session_id, self.__session_entry(session_id))
            self.registered_sid = session_id
            with self.lock:
                sessions = [session for session in self.hosts.values() if session.status == 'connected']
            for session in sessions:
                if old_sid:
                    try:
                        relay.remote_unregister(session.master, old_sid)
                    except Exception:
                        LOG.exception('%s: unregister of %s failed', session.alias, old_sid)
                for warning in self.__register_remote(session, session_id):
                    LOG.warning('%s: %s', session.alias, warning)
            for prev in previous:
                self.__move_inbox(prev, session_id)

    def wait_session_id(self, timeout: float) -> str | None:
        """Poll owner.json for the session id for up to `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while True:
            session_id = self.session_id()
            if session_id or time.monotonic() >= deadline:
                return session_id
            time.sleep(0.2)

    def live_session_ids(self) -> set[str]:
        """Return the session ids of every live Claude process with a runtime dir here."""
        live_sids = set()
        for run_dir in state.iter_runtime_dirs():
            owner = state.read_json(run_dir / 'owner.json', {})
            pid, start, session_id = owner.get('claude_pid'), owner.get('claude_proc_start'), owner.get('session_id')
            if pid and start and session_id and ident.alive(ident.ClaudeProc(int(pid), str(start))):
                live_sids.add(session_id)
        return live_sids

    #---------------------------------------------------------------------
    # Private helpers
    #---------------------------------------------------------------------

    def __move_inbox(self, old_sid: str, new_sid: str) -> None:
        """Move the unread messages of `old_sid` into the inbox of `new_sid`, addressed to it."""
        try:
            self.store.move_inbox(old_sid, new_sid)
        except Exception:
            LOG.exception('moving the messages of %s to %s failed', old_sid, new_sid)

    def __sent_rows(self, own: str) -> list[list[str]]:
        """Return a table row per recent message this session sent: recipient, time, state, start of the body.

        The state is `waiting` (unread in the recipient's inbox), `read`, or `answered` (a message
        from the recipient arrived after it). A host's states cost one ssh round trip, and an
        unreachable host shows `unknown`.
        """
        entries = self.store.sent_log(own)
        states = {}
        for alias in dict.fromkeys(entry.get('alias') for entry in entries):
            group = [entry for entry in entries if entry.get('alias') == alias]
            try:
                session = self.hosts.get(alias) if alias else None
                if alias:
                    results = relay.remote_receipts(session.master, group) if session is not None and session.status == 'connected' else ['unknown'] * len(group)
                else:
                    results = [self.store.local_receipt(entry) for entry in group]
            except Exception:
                LOG.exception('receipts of %s failed', alias)
                results = ['unknown'] * len(group)
            states.update({entry['id']: result for entry, result in zip(group, results)})
        received = self.store.read_messages(own, relay.MAX_READ_KEPT)
        rows = []
        for entry in entries:
            alias, answered = entry.get('alias'), False
            for msg in received:
                same_name = msg.get('from_name') == entry.get('to_name') and (msg.get('relayed_from') == alias if alias else msg.get('from_host') == self.hostname)
                answered = answered or (msg['id'] > entry['id'] and (msg.get('from_session') == entry['to_session'] or same_name))
            rows.append([f'{alias or "local"}/{relay.clean_text(entry.get("to_name"))}', inbox.sent_at(entry, time_only=True),
                         'answered' if answered else states[entry['id']], relay.clean_text(entry.get('preview'), 48)])
        return rows

    def __not_connected(self, tool: str, alias: str) -> str:
        """Return the error text for an alias that is not connected, naming the connected ones."""
        with self.lock:
            connected = ', '.join(sorted(self.hosts)) or 'none'
        return fmt.error(tool, alias, 'not connected', f'ask the user for the ssh host of {alias}, then call remote_connect', ('connected', connected))

    def __connected(self, tool: str, alias: str) -> tuple[HostSession | None, str]:
        """Return the connected session for `alias`, or None and the error text of `tool`."""
        with self.lock:
            session = self.hosts.get(alias)
        if session is None:
            return None, self.__not_connected(tool, alias)
        if session.status != 'connected':
            detail = fmt.block(f'error from {alias} (data)', fmt.quote(relay.clean_text(session.error, 300))) if session.error else ''
            return None, fmt.compose(fmt.error(tool, alias, session.status, 'call remote_status and retry shortly'), detail)
        return session, ''

    def __session_count(self, session: HostSession) -> str:
        """Return the number of other sessions on a connected host, `-` when unknown or messaging is off."""
        if not self.config.messaging or session.status != 'connected':
            return '-'
        try:
            return str(len([entry for entry in relay.remote_sessions(session.master, timeout=MEMORY_READ_TIMEOUT_S) if entry.get('session_id') != self.session_id()]))
        except Exception:
            return '?'

    def __disconnect_all(self, keep_in_state: bool) -> None:
        """Disconnect every host in parallel, then drop the local registration.

        Args:
            keep_in_state: Keep each host in `state.json` as "reconnecting", else leave it
                to the caller to drop.
        """
        with self.lock:
            sessions = list(self.hosts.values())
            self.hosts.clear()
        session_id = self.messaging_session_id() if self.runtime is not None else None
        store = self.__messaging_store()

        def disconnect(session: HostSession) -> None:
            locked = session.lock.acquire(timeout=TEARDOWN_LOCK_WAIT_S)
            if not locked:
                LOG.warning('%s: lifecycle step still running, disconnecting without messaging cleanup', session.alias)
            try:
                session.disconnect(store if locked else None, session_id)
            except Exception:
                LOG.exception('%s: disconnect failed', session.alias)
            finally:
                session.status = 'reconnecting'
                if locked:
                    session.lock.release()
            if keep_in_state:
                self.__save_host(session)

        threads = [threading.Thread(target=disconnect, args=(session,), name=f'disconnect-{session.alias}') for session in sessions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if session_id:
            try:
                self.store.unregister(session_id)
            except Exception:
                LOG.exception('unregister failed')
        with self._sid_lock:
            self.registered_sid = None

    def __messaging_store(self) -> relay.Store | None:
        """Return the store when messaging is on, else None."""
        return self.store if self.config.messaging else None

    def __save_host(self, session: HostSession) -> None:
        """Write one host's entry into `state.json`."""
        if self.runtime is None:
            return
        entry = session.to_state()
        self.runtime.update_state(lambda saved: saved['hosts'].__setitem__(session.alias, entry))

    def __session_entry(self, session_id: str) -> dict[str, Any]:
        """Return the registry entry of this session, without `relay_via`."""
        owner = self.runtime.owner()
        return {
            'name': owner.get('name') or relay.session_name(owner.get('cwd', ''), session_id),
            'cwd': owner.get('cwd', ''),
            'host': self.hostname,
            'pid': self.runtime.claude.pid,
            'started': self.started,
            'previous_ids': owner.get('previous_ids', []),
        }

    def __register_remote(self, session: HostSession, session_id: str | None) -> list[str]:
        """Register this session on the remote and clean stale registrations from this machine.

        Args:
            session: Connected host to register on.
            session_id: Own session id, None when SessionStart has not written it yet.

        Returns:
            Warning lines, empty on success or when messaging is off.
        """
        if not self.config.messaging:
            return []
        if not session_id:
            return ['messaging: own session id unknown, not registered on the remote']
        try:
            machine = state.machine_id()
            relay.remote_register(session.master, session_id, {**self.__session_entry(session_id), 'relay_via': machine})
            for prev in self.runtime.owner().get('previous_ids', []):
                relay.pull_outbox(session.master, prev, self.store, timeout=POLL_PULL_TIMEOUT_S)
                self.__move_inbox(prev, session_id)
            relay.remote_cleanup(session.master, machine, self.live_session_ids())
        except Exception as exc:
            LOG.exception('%s: remote registration failed', session.alias)
            return [f'messaging: remote registration failed: {exc}']
        return []

    def __connect_report(self, session: HostSession, warnings: list[str], background: bool) -> str:
        """Build the `remote_connect` result: other sessions, mounts, where Bash runs, memory.

        The header counts the sessions `remote_sessions` lists without the row marked
        `this session`. The `memory:` field is the mount path followed by the index block, or
        `none` when the remote project has no memory directory or an empty index, or
        `not mounted` when the mount is unavailable.
        """
        redirected = self.runtime.load_state().get('redirect') == session.alias
        names, count = None, None
        if self.config.messaging:
            try:
                remote = [entry for entry in relay.remote_sessions(session.master) if entry.get('session_id') != self.session_id()]
                names, count = ', '.join(relay.clean_text(entry.get('name')) for entry in remote), fmt.count(len(remote), 'other session')
            except Exception as exc:
                names = f'unavailable ({relay.clean_text(exc, 100)})'
        index = session.memory_block() if session.mounted_mem else ''
        if session.mounted_mem:
            memory_field = session.mem_dir if index else 'none'
        else:
            memory_field = 'none' if not session.mem_found else 'not mounted'
        bash = f'runs on {session.alias}' if redirected else f'runs here (remote_mode("{session.alias}") runs it on {session.alias})'
        return fmt.compose(
            fmt.head('connect', session.alias, count, 'background' if background else None),
            *fmt.fields(('path', session.abs_path), ('files', session.fs_dir if session.mounted_fs else 'not mounted'),
                        ('memory', memory_field), ('sessions', names or None), ('bash', bash)),
            *fmt.fields(*(('warning', relay.clean_text(warning, 300)) for warning in warnings)),
            index,
        )

    def __autoconnect_targets(self) -> list[tuple[str, str, str]]:
        """Return (alias, host, path) of every entry in the runtime `autoconnect.json`."""
        entries = state.read_json(self.runtime.dir / 'autoconnect.json', [])
        if not isinstance(entries, list):
            return []
        return [(entry['alias'], entry['host'], entry.get('path', '~')) for entry in entries
                if isinstance(entry, dict) and 'alias' in entry and 'host' in entry]

    def __connect_all(self, targets: list[tuple[str, str, str]]) -> None:
        """Start a background connect per distinct alias that is not connected yet."""
        seen = set()
        for alias, host, path in targets:
            with self.lock:
                connected = alias in self.hosts
            if alias in seen or connected:
                continue
            seen.add(alias)
            threading.Thread(target=self.__connect_background, args=(alias, host, path), daemon=True).start()

    def __connect_background(self, alias: str, host: str, path: str) -> None:
        """Connect one host from startup recovery or autoconnect and queue its report for the session.

        No tool result carries a background connect's report, so the next UserPromptSubmit
        or PostToolUse hook injects it: mount paths and the remote memory index.
        """
        text, is_error = self.connect_host(alias, host, path, background=True)
        (LOG.warning if is_error else LOG.info)('background connect %s: %s', alias, text.splitlines()[0])
        if not is_error:
            self.runtime.queue_context(alias, text)

    def __recover_previous(self) -> list[tuple[str, str, str]]:
        """Release whatever a previous server left in this runtime dir.

        Returns:
            (alias, host, path) of every host to reconnect, failed ones excluded.
        """
        targets = []
        for alias, entry in self.runtime.load_state()['hosts'].items():
            if not entry.get('host'):
                continue
            host_dir = self.runtime.host_dir(alias)
            for sub in ('mem', 'fs'):
                mount.unmount(host_dir / sub)
            Master(alias, entry['host'], self.runtime.ctl_path(alias), self.config.ssh_opts).stop()
            if entry.get('status') != 'failed':
                targets.append((alias, entry['host'], entry.get('path') or '~'))
        return targets

    def __tick_host(self, session: HostSession, store: relay.Store | None, session_id: str | None) -> None:
        """Check one host, relay its outbox and reconnect it when due, in a thread of its own.

        One ssh process per host and tick: with messaging on, the outbox pull doubles as
        the health check, behind an in-process probe of the control socket, since ssh
        falls back to a direct connection when the master is dead. With messaging off,
        `-O check` asks the master over its local socket. A failed probe marks the host
        for reconnect only when the master process has exited or `HEALTH_FAILURE_LIMIT`
        ticks in a row failed, so one slow answer never tears down a working master.
        The caller acquired `session.lock`, and this method releases it on every path.
        """
        reconnect_due = False
        try:
            now = time.monotonic()
            if session.status == 'connected':
                if store is not None and session_id:
                    healthy = session.master.is_listening() and self.__pull(session, session_id, store)
                else:
                    healthy = session.master.check()
                session.failures = 0 if healthy else session.failures + 1
                if not healthy and (session.master.exited() or session.failures >= HEALTH_FAILURE_LIMIT):
                    LOG.warning('%s: master check failed %d times, reconnecting', session.alias, session.failures)
                    session.failures = 0
                    session.status, session.retry_at, session.retry_delay = 'reconnecting', now, 1.0
                    self.__save_host(session)
            reconnect_due = session.status == 'reconnecting' and now >= session.retry_at
        except Exception:
            LOG.exception('%s: tick failed', session.alias)
        if reconnect_due:
            self.__reconnect(session)
        else:
            session.lock.release()

    def __pull(self, session: HostSession, session_id: str, store: relay.Store) -> bool:
        """Pull the outbox of one host and return False when ssh failed (exit 255) or timed out.

        `POLL_PULL_TIMEOUT_S` bounds how long a frozen host holds its lock before it is
        marked for reconnect.
        """
        try:
            relay.pull_outbox(session.master, session_id, store, timeout=POLL_PULL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            LOG.warning('%s: outbox pull timed out after %.0f s', session.alias, POLL_PULL_TIMEOUT_S)
            return False
        except relay.RemoteCommandError as exc:
            if exc.returncode == SSH_ERROR_EXIT:
                return False
            LOG.warning('%s: outbox pull failed: %s', session.alias, exc)
        return True

    def __reconnect(self, session: HostSession) -> None:
        """Try one master restart with backoff, then release `session.lock`, which the caller holds."""
        try:
            session.reconnect()
            session.retry_delay = 1.0
            LOG.info('%s: reconnected', session.alias)
            with self.lock:
                dropped = self.hosts.get(session.alias) is not session
            if dropped:
                LOG.info('%s: disconnected during the reconnect, releasing it', session.alias)
                session.release()
        except Exception as exc:
            session.error = str(exc)
            session.retry_at = time.monotonic() + session.retry_delay
            session.retry_delay = min(session.retry_delay * 2, BACKOFF_MAX_S)
            LOG.warning('%s: reconnect failed, next try in %.0f s: %s', session.alias, session.retry_at - time.monotonic(), exc)
        finally:
            try:
                with self.lock:
                    tracked = self.hosts.get(session.alias) is session
                if tracked:
                    self.__save_host(session)
            except Exception:
                LOG.exception('%s: saving the reconnect result failed', session.alias)
            finally:
                session.lock.release()


#---------------------------------------------------------------------
# JSON-RPC layer
#---------------------------------------------------------------------

def rpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    """Return a JSON-RPC 2.0 error response."""
    return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': code, 'message': message}}


def handle_message(server: Server, msg: Any) -> dict[str, Any] | None:
    """Answer one JSON-RPC message.

    Args:
        server: Tool implementation.
        msg: Decoded JSON message.

    Returns:
        The response, or None for notifications and stray responses.
    """
    if not isinstance(msg, dict) or not isinstance(msg.get('method'), str):
        if isinstance(msg, dict) and 'method' not in msg and ('result' in msg or 'error' in msg):
            return None
        return rpc_error(msg.get('id') if isinstance(msg, dict) else None, -32600, 'invalid request')
    method, params = msg['method'], msg.get('params') or {}
    if 'id' not in msg:
        return None
    msg_id = msg['id']
    if method == 'initialize':
        return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
            'protocolVersion': params.get('protocolVersion') or DEFAULT_PROTOCOL,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'remote-ssh', 'version': __version__},
        }}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': msg_id, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': msg_id, 'result': {'tools': TOOLS}}
    if method == 'tools/call':
        name = params.get('name')
        if name not in TOOL_NAMES:
            return rpc_error(msg_id, -32602, f'unknown tool: {name}')
        text, is_error = server.call_tool(name, params.get('arguments') or {})
        return {'jsonrpc': '2.0', 'id': msg_id, 'result': {'content': [{'type': 'text', 'text': text}], 'isError': is_error}}
    return rpc_error(msg_id, -32601, f'method not found: {method}')


def serve(stdin: IO[str], stdout: IO[str], server: Server) -> None:
    """Serve newline-delimited JSON-RPC until EOF on `stdin`.

    `tools/call` requests run in worker threads, so a long `remote_exec` never blocks
    `ping` or other calls. At EOF, outstanding workers get `WORKER_JOIN_S` to finish.

    Args:
        stdin: Text stream of requests, one JSON object per line.
        stdout: Text stream for responses. Nothing else is ever written to it.
        server: Tool implementation.
    """
    write_lock = threading.Lock()

    def send(reply: dict[str, Any] | None) -> None:
        if reply is None:
            return
        with write_lock:
            stdout.write(json.dumps(reply) + '\n')
            stdout.flush()

    workers = []
    while True:
        line = stdin.readline()
        if not line:
            break
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send(rpc_error(None, -32700, 'parse error'))
            continue
        if isinstance(msg, dict) and msg.get('method') == 'tools/call' and 'id' in msg:
            worker = threading.Thread(target=lambda request=msg: send(handle_message(server, request)), daemon=True)
            worker.start()
            workers.append(worker)
            continue
        send(handle_message(server, msg))
    deadline = time.monotonic() + WORKER_JOIN_S
    for worker in workers:
        worker.join(timeout=max(deadline - time.monotonic(), 0))


#---------------------------------------------------------------------
# Entry point
#---------------------------------------------------------------------

def open_runtime() -> tuple[state.Runtime | None, str]:
    """Return the runtime of the owning Claude process, or None and why it is unavailable."""
    try:
        runtime = state.Runtime.current()
    except Exception as exc:
        sys.stderr.write(f'remote-ssh: runtime dir unavailable: {exc}\n')
        return None, f'runtime directory unavailable: {exc}'
    return runtime, NO_RUNTIME


def main() -> None:
    """Run the MCP server on the process's stdin and stdout until a shutdown trigger."""
    runtime, runtime_error = open_runtime()
    if runtime is not None:
        logging.basicConfig(filename=str(runtime.dir / 'server.log'), level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(threadName)s %(message)s')
    else:
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    server = Server(runtime, Config.from_env(), runtime_error=runtime_error)

    def on_signal(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, name=f'signal-{signum}', daemon=True).start()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, on_signal)
    if runtime is not None:
        server.start()
    serve(sys.stdin, sys.stdout, server)
    server.shutdown()
