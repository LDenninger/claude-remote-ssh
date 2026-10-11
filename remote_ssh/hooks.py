"""Hook handlers of the remote-ssh plugin.

Every hook event runs `main(event)`, which reads the event's JSON from stdin, dispatches
it through `dispatch` and prints the hook output JSON, or nothing. Hooks never talk to the
MCP server directly: they read and write the runtime directory (`owner.json`,
`state.json`, `autoconnect.json`, `shutdown`, `context/`) and the local message store.
Every failure fails open, logged to stderr, with exit code 0. Each handler imports only
the modules it needs, since hooks run on every prompt and tool call.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

SHUTDOWN_WAIT_S = 10.0
SHUTDOWN_POLL_S = 0.2
LOCAL_PREFIX = 'local:'


#---------------------------------------------------------------------
# Helpers
#---------------------------------------------------------------------

def build_output(event: str, additional_context: str = '', system_message: str = '', **specific: Any) -> dict[str, Any] | None:
    """Return the hook output JSON with empty fields omitted, None when nothing is left.

    Args:
        event: Hook event name, repeated as `hookEventName`.
        additional_context: Text added to Claude's context, omitted when empty.
        system_message: Text shown to the user, omitted when empty.
        **specific: Further `hookSpecificOutput` fields, omitted when None.
    """
    hook_specific = {key: value for key, value in specific.items() if value is not None}
    if additional_context:
        hook_specific['additionalContext'] = additional_context
    output: dict[str, Any] = {}
    if hook_specific:
        output['hookSpecificOutput'] = {'hookEventName': event, **hook_specific}
    if system_message:
        output['systemMessage'] = system_message
    return output or None


def is_option_enabled(name: str, default: bool = True) -> bool:
    """Return a boolean plugin option from `CLAUDE_PLUGIN_OPTION_<name>`.

    Args:
        name: Option key in upper case, e.g. `REDIRECT_CONFIRM`.
        default: Value when the variable is unset or empty.
    """
    value = os.environ.get(f'CLAUDE_PLUGIN_OPTION_{name}', '').strip().lower()
    if not value:
        return default
    return value not in ('false', '0', 'no', 'off')


def is_pid_alive(pid: Any) -> bool:
    """Return whether a process with this pid exists."""
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


#---------------------------------------------------------------------
# Event handlers
#---------------------------------------------------------------------

def on_session_start(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Record the session identity, hand autoconnect hosts to the server, inject memory."""
    import socket
    from pathlib import Path

    from remote_ssh import memory, platform, relay, state

    runtime = state.Runtime.current()
    if runtime is None:
        return None
    session_id = payload.get('session_id', '')
    cwd = payload.get('cwd') or os.getcwd()

    #--- autoconnect.json first: the session id in owner.json is the server's signal ---
    config = state.read_json(Path(cwd) / '.claude' / 'remote-ssh.json', {})
    config_hosts = config.get('hosts', []) if isinstance(config, dict) else []
    autoconnect = [
        {'alias': entry['alias'], 'host': entry['host'], 'path': entry.get('path', '~')}
        for entry in config_hosts
        if isinstance(entry, dict) and entry.get('autoconnect') and entry.get('alias') and entry.get('host')
    ]
    state.write_json_atomic(runtime.dir / 'autoconnect.json', autoconnect)
    runtime.update_owner(
        session_id=session_id,
        cwd=cwd,
        name=relay.session_name(cwd, session_id),
        claude_pid=runtime.claude.pid,
        claude_proc_start=runtime.claude.start,
        hostname=socket.gethostname(),
    )

    #--- context: memory of hosts still connected (resume), and the autoconnect note ---
    blocks = []
    for alias, entry in runtime.load_state()['hosts'].items():
        if entry.get('status') == 'connected' and entry.get('mounted_mem'):
            mem_dir = runtime.mem_path(alias)
            try:
                index_md = (mem_dir / 'MEMORY.md').read_text(encoding='utf-8', errors='replace')
            except OSError:
                index_md = None
            blocks.append(memory.render_block(alias, index_md, mem_dir if index_md is not None else None))
    if autoconnect:
        aliases = ', '.join(entry['alias'] for entry in autoconnect)
        blocks.append(f'remote-ssh: connecting {aliases} in the background. Mounts and remote memory arrive with the next prompt or tool call.')
    return build_output('SessionStart', '\n\n'.join(blocks), platform.missing_message(platform.missing(platform.detect())))


def on_session_end(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Ask the server to tear down and wait up to `SHUTDOWN_WAIT_S` for it to finish.

    A `/clear` also fires SessionEnd while the same Claude process continues, so that
    reason keeps every connection.
    """
    if payload.get('reason') == 'clear':
        return None
    import time

    from remote_ssh import state

    runtime = state.Runtime.current()
    if runtime is None:
        return None
    server_pid = runtime.owner().get('server_pid')
    if not server_pid or not is_pid_alive(server_pid):
        return None
    runtime.shutdown_path.touch()
    deadline = time.monotonic() + SHUTDOWN_WAIT_S
    while runtime.shutdown_path.exists() and time.monotonic() < deadline:
        time.sleep(SHUTDOWN_POLL_S)
    return None


def on_pre_tool_use(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Rewrite a Bash call into an ssh call on the redirect target, if one is connected."""
    if payload.get('tool_name') != 'Bash':
        return None
    import shlex

    from remote_ssh import state
    from remote_ssh.conn import exec_argv

    runtime = state.Runtime.current()
    if runtime is None:
        return None
    try:
        saved = runtime.load_state()
        target = saved.get('redirect')
        entry = saved['hosts'].get(target) if target else None
    except Exception as exc:
        sys.stderr.write(f'remote-ssh: {exc}\n')
        return build_output('PreToolUse', 'remote-ssh: redirect state unreadable, command runs locally')
    if not entry or entry.get('status') != 'connected':
        return None
    tool_input = payload.get('tool_input') or {}
    command = tool_input.get('command', '')
    stripped = command.lstrip()
    if stripped.startswith(LOCAL_PREFIX):
        return build_output('PreToolUse', updatedInput={**tool_input, 'command': stripped[len(LOCAL_PREFIX):].lstrip()})
    #--- hooks never see the MCP server's env: use the ssh options the server saved ---
    extra = saved['ssh_opts'] if 'ssh_opts' in saved else shlex.split(os.environ.get('REMOTE_SSH_SSH_OPTS', ''))
    argv = exec_argv(runtime.ctl_path(target), entry['host'], command, entry.get('path'), extra=extra or None)
    updated_input = {**tool_input, 'command': shlex.join(argv)}
    if not is_option_enabled('REDIRECT_CONFIRM'):
        return build_output('PreToolUse', updatedInput=updated_input)
    return build_output('PreToolUse', permissionDecision='ask', permissionDecisionReason=f'remote-ssh: runs on {target}', updatedInput=updated_input)


def on_message_check(payload: dict[str, Any], event: str) -> dict[str, Any] | None:
    """Inject queued host context blocks and undelivered messages (UserPromptSubmit, PostToolUse)."""
    from remote_ssh import inbox, relay, state

    runtime = state.Runtime.current()
    blocks = runtime.claim_context() if runtime is not None else []
    session_id = payload.get('session_id')
    msgs = inbox.claim_pending(relay.Store(), session_id) if session_id else []
    if msgs:
        blocks.append(inbox.format_batch(msgs))
    return build_output(event, '\n\n'.join(blocks)) if blocks else None


#---------------------------------------------------------------------
# Entry point
#---------------------------------------------------------------------

def dispatch(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Run the handler of one hook event.

    Args:
        event: Hook event name as passed on the command line.
        payload: The event's stdin JSON.

    Returns:
        The hook output JSON, or None for no output.
    """
    if event == 'SessionStart':
        return on_session_start(payload)
    if event == 'SessionEnd':
        return on_session_end(payload)
    if event == 'PreToolUse':
        return on_pre_tool_use(payload)
    if event in ('UserPromptSubmit', 'PostToolUse'):
        return on_message_check(payload, event)
    return None


def main(event: str) -> None:
    """Handle one hook invocation; never raises and never blocks the session.

    Args:
        event: Hook event name, e.g. `PreToolUse`.
    """
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        output = dispatch(event, payload if isinstance(payload, dict) else {})
        if output is not None:
            sys.stdout.write(json.dumps(output) + '\n')
            sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f'remote-ssh hook {event}: {exc!r}\n')
