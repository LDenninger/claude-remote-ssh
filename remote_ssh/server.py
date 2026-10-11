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

from remote_ssh import __version__, ident, inbox, memory, mount, platform, relay, state
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
TEARDOWN_MESSAGING_TIMEOUT_S = 3.0
TEARDOWN_LOCK_WAIT_S = 2.0


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
        try:
            poll_interval_s = min(max(float(os.environ.get('REMOTE_SSH_POLL_INTERVAL') or 2), 1.0), 60.0)
        except ValueError:
            poll_interval_s = 2.0
        return cls(
            messaging=(os.environ.get('REMOTE_SSH_MESSAGING') or 'true').lower() not in ('false', '0'),
            poll_interval_s=poll_interval_s,
            sshfs_options=os.environ.get('REMOTE_SSH_SSHFS_OPTIONS', ''),
            ssh_opts=shlex.split(os.environ.get('REMOTE_SSH_SSH_OPTS', '')),
        )


#---------------------------------------------------------------------
# Tool schemas
#---------------------------------------------------------------------

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
        'description': 'List hosts, mounts, redirect target and missing local tools.',
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
                'timeout_s': {'type': 'integer', 'minimum': 1, 'default': 600},
            },
            'required': ['alias', 'command'],
        },
    },
    {
        'name': 'remote_mode',
        'description': 'Run later Bash tool calls on a host, or locally again with null.',
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
            },
            'required': ['session', 'body'],
        },
    },
    {
        'name': 'remote_inbox',
        'description': 'Show new and recent messages for this session.',
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

    def disconnect(self, store: relay.Store | None, session_id: str | None) -> list[str]:
        """Unmount, pull pending replies, unregister this session remotely, stop the master.

        The local steps come first and are bounded. The messaging steps need the remote, so
        they run only while the master's socket is listening and give up after
        `TEARDOWN_MESSAGING_TIMEOUT_S` each, so a frozen host never stalls teardown.

        Args:
            store: Local message store, None when messaging is off.
            session_id: Own session id, None when unknown.

        Returns:
            Report lines, one per step that did something or failed.
        """
        report = self.unmount_all()
        if store is not None and session_id and self.status == 'connected' and self.master.is_listening():
            try:
                pulled = relay.pull_outbox(self.master, session_id, store, timeout=TEARDOWN_MESSAGING_TIMEOUT_S)
                report.append(f'pulled {pulled} pending message(s)')
                relay.remote_unregister(self.master, session_id, timeout=TEARDOWN_MESSAGING_TIMEOUT_S)
                report.append('unregistered this session on the remote')
            except Exception as exc:
                LOG.warning('%s: messaging teardown failed: %r', self.alias, exc)
                report.append(f'messaging teardown failed: {exc!r}')
        report.extend(self.release())
        return report

    def unmount_all(self) -> list[str]:
        """Unmount memory and files. Never raises.

        Returns:
            Report lines for each mount that was mounted.
        """
        report = []
        for name, mount_dir in (('memory', self.mem_dir), ('files', self.fs_dir)):
            if mount.is_mounted(mount_dir):
                unmounted = mount.unmount(mount_dir)
                report.append(f'{name}: {"unmounted" if unmounted else "UNMOUNT FAILED"} {mount_dir}')
        self.mounted_fs = self.mounted_mem = False
        return report

    def release(self) -> list[str]:
        """Unmount memory and files, then stop the master. Never raises.

        Returns:
            Report lines for each mount and the master.
        """
        report = self.unmount_all()
        try:
            self.master.stop()
            report.append('master stopped')
        except Exception as exc:
            LOG.exception('%s: master stop failed', self.alias)
            report.append(f'master stop failed: {exc}')
        return report

    def is_healthy(self) -> bool:
        """Return whether the ControlMaster still answers `-O check`."""
        return self.master.check()

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
        """Return the remote memory block, with links rewritten to the memory mount."""
        if not self.mounted_mem:
            return memory.render_block(self.alias, None, None)
        index_path = self.mem_dir / 'MEMORY.md'
        try:
            index_md = index_path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return memory.render_block(self.alias, None, None)
        return memory.render_block(self.alias, index_md, self.mem_dir)

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

    #---------------------------------------------------------------------
    # Tool dispatch
    #---------------------------------------------------------------------

    def call_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool and return its text result.

        Args:
            name: Tool name from `TOOLS`.
            args: Tool arguments as sent by the client.

        Returns:
            The result text, whose first line is a one-line status, and whether it is an error.
        """
        if self.runtime is None:
            return f'{name} failed: {self.runtime_error}', True
        schema = next(tool['inputSchema'] for tool in TOOLS if tool['name'] == name)
        missing_args = [key for key in schema.get('required', []) if key not in args]
        if missing_args:
            return f'{name} failed: missing argument(s) {", ".join(missing_args)}', True
        try:
            return getattr(self, f'tool_{name}')(args)
        except Exception as exc:
            LOG.exception('tool %s failed', name)
            return f'{name} failed: {exc}', True

    def tool_remote_connect(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Connect `args.alias` to `args.host` at `args.path`."""
        return self.connect_host(args['alias'], args['host'], args['path'])

    def tool_remote_disconnect(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Disconnect `args.alias`."""
        return self.disconnect_host(args['alias'])

    def tool_remote_status(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Report every host, the redirect target and missing local tools."""
        saved = self.runtime.load_state()
        with self.lock:
            sessions = dict(self.hosts)
        lines = []
        for alias, session in sorted(sessions.items()):
            master = 'up' if session.status == 'connected' and session.is_healthy() else 'down'
            lines.append(f'{alias}: {session.status}, {session.host}:{session.abs_path or session.path}, master {master}')
            lines.append(f'  files: {session.fs_dir if session.mounted_fs else "not mounted"}')
            lines.append(f'  memory: {session.mem_dir if session.mounted_mem else "not mounted"}')
            if session.error:
                lines.append(f'  note: {session.error}')
        for alias, entry in sorted(saved['hosts'].items()):
            if alias not in sessions:
                lines.append(f'{alias}: {entry.get("status")}, {entry.get("host")}:{entry.get("path")}, error: {entry.get("error")}')
        redirect = saved.get('redirect')
        lines.append(f'redirect: {redirect or "off (Bash runs locally)"}')
        lines.append(f'messaging: {"on" if self.config.messaging else "off"}, session {self.session_id() or "unknown"}')
        missing_text = platform.missing_message(platform.missing(self.tools))
        if missing_text:
            lines.append(missing_text)
        status = f'{len(sessions)} host(s) connected' if sessions else 'no hosts connected'
        return '\n'.join([status, *lines]), False

    def tool_remote_exec(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Run `args.command` on `args.alias`."""
        session, error = self.__connected(args['alias'])
        if session is None:
            return error, True
        timeout_s = int(args.get('timeout_s') or 600)
        stdout, stderr, code = session.master.exec(args['command'], args.get('cwd') or session.abs_path, timeout=timeout_s)
        sections = [f'--- {name} ---\n{text}' for name, text in (('stdout', stdout), ('stderr', stderr)) if text]
        return '\n'.join([f'exit {code} on {session.alias}', *sections]), code != 0

    def tool_remote_mode(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Set or clear the Bash redirect target."""
        alias = args.get('alias')
        if alias:
            session, error = self.__connected(alias)
            if session is None:
                return error, True
        self.runtime.update_state(lambda saved: saved.update(redirect=alias or None))
        if alias:
            return f'redirect on: Bash runs on {alias}\nPrefix a command with "local:" to run it on this machine.', False
        return 'redirect off: Bash runs locally', False

    def tool_remote_sessions(self, args: dict[str, Any]) -> tuple[str, bool]:
        """List sessions registered on `args.alias`, or locally without alias."""
        alias = args.get('alias')
        if alias:
            session, error = self.__connected(alias)
            if session is None:
                return error, True
            sessions, where = relay.remote_sessions(session.master), alias
        else:
            sessions, where = self.store.sessions(), 'this machine'
        own = self.session_id()
        lines = [f'{len(sessions)} session(s) on {where}']
        for entry in sessions:
            marker = ' (this session)' if entry.get('session_id') == own else ''
            via = f' via {entry["relay_via"]}' if entry.get('relay_via') else ''
            lines.append(f'{entry.get("name")}  {entry.get("session_id")}  {entry.get("host")}:{entry.get("cwd")}{via}{marker}')
        return '\n'.join(lines), False

    def tool_remote_send(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Send `args.body` to `args.session`, on `args.alias` or locally."""
        own = self.session_id()
        if not own:
            return 'remote_send failed: own session id unknown (SessionStart hook has not run)', True
        if not self.config.messaging:
            return 'remote_send failed: messaging is disabled in the plugin options', True
        alias = args.get('alias')
        session = None
        if alias:
            session, error = self.__connected(alias)
            if session is None:
                return error, True
            sessions = relay.remote_sessions(session.master)
        else:
            sessions = self.store.sessions()
        target = relay.resolve(sessions, args['session'])
        if target is None:
            return f'remote_send failed: no session {args["session"]!r} on {alias or "this machine"}', True
        body_bytes = len(args['body'].encode('utf-8'))
        if body_bytes > relay.MAX_BODY_BYTES:
            return f'remote_send failed: body is {body_bytes} bytes, the limit is {relay.MAX_BODY_BYTES}; shorten it or send a file path', True
        owner = self.runtime.owner()
        msg = relay.new_message(own, owner.get('name', ''), self.hostname, target['session_id'], args['body'])
        if session is not None:
            relay.remote_deliver(session.master, msg)
            where = f'inbox on {alias}'
        else:
            where = self.store.send(msg)
        return f'sent {msg["id"]} to {target.get("name")} ({target["session_id"]}) via {where}', False

    def tool_remote_inbox(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Claim and show new messages, then list recently delivered ones."""
        own = self.session_id()
        if not own:
            return 'remote_inbox failed: own session id unknown (SessionStart hook has not run)', True
        new_msgs = inbox.claim_pending(self.store, own)
        delivered = self.store.read_messages(own)
        lines = [f'{len(new_msgs)} new, {len(delivered)} delivered message(s)']
        if new_msgs:
            lines.append(inbox.format_batch(new_msgs))
        if delivered:
            lines.append('--- delivered ---')
            lines.append(inbox.format_batch(delivered))
        return '\n'.join(lines), False

    #---------------------------------------------------------------------
    # Connect and disconnect
    #---------------------------------------------------------------------

    def connect_host(self, alias: str, host: str, path: str) -> tuple[str, bool]:
        """Connect one host and record it in `state.json`.

        Returns:
            The `remote_connect` result text and whether it is an error.
        """
        try:
            alias = state.validate_alias(alias)
        except ValueError as exc:
            return f'remote_connect failed: {exc}', True
        if not host or host.startswith('-'):
            return f'remote_connect failed: invalid host {host!r}: it must be non-empty and must not start with -', True
        with self.lock:
            existing = self.hosts.get(alias)
            if existing is not None:
                return f'remote_connect failed: {alias} is already {existing.status} ({existing.host}); disconnect it first', True
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
                return f'remote_connect failed: {alias} ({host})\n{exc}', True
            warnings.extend(self.__register_remote(session, self.messaging_session_id()))
            self.__save_host(session)
        return self.__connect_report(session, warnings), False

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
                return f'removed {alias} (was not connected)', False
            return f'remote_disconnect failed: {alias} is not connected', True
        with session.lock:
            report = session.disconnect(self.__messaging_store(), self.messaging_session_id())
        self.runtime.update_state(drop)
        self.runtime.drop_context(alias)
        lines = [f'disconnected {alias}', *report, f'Paths under {session.fs_dir.parent} no longer exist; earlier remote memory links are stale.']
        return '\n'.join(lines), False

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
            self.runtime.update_state(lambda saved: saved.update(poll_interval_s=self.config.poll_interval_s, ssh_opts=self.config.ssh_opts))
            self.sweep()
            targets = self.__recover_previous()
            self.wait_session_id(OWNER_WAIT_S)
            self.sync_session_id()
            autoconnect = state.read_json(self.runtime.dir / 'autoconnect.json', [])
            self.runtime.update_owner(autoconnect_read=True)
            targets.extend((entry['alias'], entry['host'], entry.get('path', '~')) for entry in autoconnect if isinstance(entry, dict) and 'alias' in entry and 'host' in entry)
        except Exception:
            LOG.exception('startup failed')
            return
        seen = set()
        for alias, host, path in targets:
            if alias in seen:
                continue
            seen.add(alias)
            threading.Thread(target=self.__connect_background, args=(alias, host, path), daemon=True).start()

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

    def update_pending_flag(self) -> None:
        """Create the pending flag while this session's inbox holds messages, remove it otherwise."""
        try:
            session_id = self.session_id()
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
            if not pid or not start:
                continue
            if ident.alive(ident.ClaudeProc(int(pid), str(start))):
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

        `/clear` keeps the Claude process and the server but makes SessionStart write a
        new session id. The old id is unregistered locally and on every connected host,
        and the new one registered in its place. Does nothing with messaging off or while
        the id is unknown or unchanged.
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

    def __connected(self, alias: str) -> tuple[HostSession | None, str]:
        """Return the connected session for `alias`, or None and an error line.

        The error for an unknown alias names the connected aliases.
        """
        with self.lock:
            session = self.hosts.get(alias)
            connected = ', '.join(sorted(self.hosts)) or 'none'
        if session is None:
            return None, f'{alias} is not connected. Connected hosts: {connected}. Ask the user for the ssh host of {alias}, then call remote_connect.'
        if session.status != 'connected':
            return None, f'{alias} is {session.status}: {session.error or "try again shortly"}'
        return session, ''

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
            relay.remote_cleanup(session.master, machine, self.live_session_ids())
        except Exception as exc:
            LOG.exception('%s: remote registration failed', session.alias)
            return [f'messaging: remote registration failed: {exc}']
        return []

    def __connect_report(self, session: HostSession, warnings: list[str]) -> str:
        """Build the `remote_connect` result: mounts, remote sessions, where Bash runs, memory block."""
        lines = [f'connected {session.alias} ({session.host}:{session.abs_path})']
        lines.append(f'files: {session.fs_dir if session.mounted_fs else "not mounted"}')
        lines.append(f'memory: {session.mem_dir if session.mounted_mem else "not mounted"}')
        if self.config.messaging:
            try:
                remote = [entry for entry in relay.remote_sessions(session.master) if entry.get('session_id') != self.session_id()]
                names = ', '.join(f'{entry.get("name")} ({str(entry.get("session_id"))[:8]})' for entry in remote)
                lines.append(f'sessions on {session.alias}: {names or "none"}')
            except Exception as exc:
                lines.append(f'sessions on {session.alias}: unavailable ({exc})')
        if self.runtime.load_state().get('redirect') == session.alias:
            lines.append(f'Bash runs on {session.alias} (redirect on).')
        else:
            lines.append(f'Bash runs locally. remote_exec runs one command on {session.alias}, '
                         f'remote_mode("{session.alias}") sends every later Bash call there.')
        lines.extend(f'warning: {warning}' for warning in warnings)
        lines.extend(['', session.memory_block()])
        return '\n'.join(lines)

    def __connect_background(self, alias: str, host: str, path: str) -> None:
        """Connect one host from startup recovery or autoconnect and queue its report for the session.

        No tool result carries a background connect's report, so the next UserPromptSubmit
        or PostToolUse hook injects it: mount paths and the remote memory index.
        """
        text, is_error = self.connect_host(alias, host, path)
        (LOG.warning if is_error else LOG.info)('background connect %s: %s', alias, text.splitlines()[0])
        if not is_error:
            self.runtime.queue_context(alias, f'remote-ssh: background connect\n{text}')

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
        `-O check` asks the master over its local socket. The caller acquired
        `session.lock`, and this method releases it on every path.
        """
        reconnect_due = False
        try:
            now = time.monotonic()
            if session.status == 'connected':
                if store is not None and session_id:
                    healthy = session.master.is_listening() and self.__pull(session, session_id, store)
                else:
                    healthy = session.is_healthy()
                if not healthy:
                    LOG.warning('%s: master check failed, reconnecting', session.alias)
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
