"""Hook handlers of the remote-ssh plugin.

Every hook event runs `main(event)`, which reads the event's JSON from stdin, dispatches
it through `dispatch` and prints the hook output JSON, or nothing. Hooks never talk to the
MCP server directly: they read and write the runtime directory (`owner.json`,
`state.json`, `autoconnect.json`, `shutdown`, `context/`) and the local message store.
Every failure is logged to stderr with exit code 0 and fails open, except a redirected
PreToolUse, which fails closed. Each handler imports only the modules it needs, since hooks
run on every prompt and tool call.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

LOCAL_PREFIX = 'local:'
AUTOCONNECT_FILE = 'autoconnect.json'
REPO_CONFIG_FILES = ('remote-ssh.json', 'remote-ssh.local.json')
KEEP_REASONS = ('clear', 'resume')
MISSING_TOOLS_PERIOD_S = 86400
REASON_COMMAND_CHARS = 300
DEFAULT_BASH_TIMEOUT_S = 120
MAX_BASH_TIMEOUT_S = 600
#--- command separators, subshell and substitution openers/closers: splitting at all of them,
#--- quoted or not, can only produce more segments and so more deny matches ---
SEGMENT_SPLIT = re.compile(r'&&|\|\||\|&|[;|&\n`()]|\$\(|\$\{|\{|\}')
#--- shell keywords and wrappers that Claude Code strips before it matches a Bash rule ---
LEADING_WORDS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', 'time', 'command', 'builtin', 'noglob', 'nohup', 'nice', 'timeout', 'stdbuf', 'xargs', 'exec'}
ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=\S*$')


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
    """Record the session identity, hand autoconnect hosts to the server, inject memory.

    Autoconnect hosts come only from `autoconnect.json` in the message base
    (`~/.claude/remote-ssh/` unless `REMOTE_SSH_MSG_BASE` is set), under
    `projects.<absolute project path>.hosts`. A file inside the repository would let any cloned
    repository open ssh connections, so `.claude/remote-ssh.json` and
    `.claude/remote-ssh.local.json` are ignored, with a note once per session.
    """
    import socket
    from pathlib import Path

    from remote_ssh import fmt, memory, relay, state

    runtime = state.Runtime.current()
    if runtime is None:
        return None
    session_id = payload.get('session_id', '')
    cwd = payload.get('cwd') or os.getcwd()
    owner = runtime.owner()

    #--- autoconnect.json first: the session id in owner.json is the server's signal ---
    config_path = state.msg_base() / AUTOCONNECT_FILE
    config = state.read_json(config_path, {})
    projects = config.get('projects') if isinstance(config, dict) else None
    project = projects.get(cwd) if isinstance(projects, dict) else None
    config_hosts = project.get('hosts') if isinstance(project, dict) else None
    autoconnect = [
        {'alias': entry['alias'], 'host': entry['host'], 'path': entry.get('path', '~')}
        for entry in (config_hosts if isinstance(config_hosts, list) else [])
        if isinstance(entry, dict) and entry.get('alias') and entry.get('host')
    ]
    state.write_json_atomic(runtime.dir / 'autoconnect.json', autoconnect)
    repo_config = [name for name in REPO_CONFIG_FILES if (Path(cwd) / '.claude' / name).exists()]
    note_shared = bool(repo_config) and owner.get('shared_config_noted') != session_id
    #--- compact keeps the session id, so the name the server took over from an earlier session stays ---
    name = owner.get('name') if owner.get('session_id') == session_id and owner.get('name') else relay.session_name(cwd, session_id)
    runtime.update_owner(
        session_id=session_id,
        cwd=cwd,
        name=name,
        claude_pid=runtime.claude.pid,
        claude_proc_start=runtime.claude.start,
        hostname=socket.gethostname(),
        **({'shared_config_noted': session_id} if note_shared else {}),
    )

    #--- context: memory of hosts still connected (resume), and the autoconnect note ---
    blocks = []
    for alias, entry in runtime.load_state()['hosts'].items():
        if entry.get('status') == 'connected' and entry.get('mounted_mem'):
            #--- the server's copy of the index: a read through the sshfs mount blocks on a hung host ---
            try:
                index_md = memory.cache_path(runtime.dir, alias).read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            blocks.append(fmt.compose(fmt.head('memory', alias, 'index'), *fmt.fields(('memory', runtime.mem_path(alias))),
                                      memory.render_block(alias, index_md)))
    #--- the server reads autoconnect.json once per process, and marks owner.json when it has ---
    if autoconnect and not owner.get('autoconnect_read'):
        aliases = ', '.join(entry['alias'] for entry in autoconnect)
        blocks.append(fmt.compose(fmt.head('connect', None, f'{aliases} in the background'),
                                  'next: wait for the mounts and remote memory, they arrive with the next prompt or tool call'))
    if note_shared:
        blocks.append(fmt.compose(fmt.head('connect', None, 'repository config ignored'),
                                  *fmt.fields(('reason', f'.claude/{" and .claude/".join(repo_config)} is a repository file and must not open ssh connections'),
                                              ('next', f'put autoconnect hosts in {config_path} as '
                                                       '{"projects": {"<absolute project path>": {"hosts": [{"alias", "host", "path"}]}}}'))))
    return build_output('SessionStart', '\n\n'.join(blocks), missing_tools_message())


def missing_tools_message() -> str:
    """Return the install hints for missing local tools at most once per day per machine.

    The time of the last hint is the mtime of `msg_base()/missing-tools-shown`.
    """
    import time

    from remote_ssh import platform, state

    text = platform.missing_message(platform.missing(platform.detect()))
    if not text:
        return ''
    stamp_path = state.ensure_private_dir(state.msg_base()) / 'missing-tools-shown'
    try:
        if time.time() - stamp_path.stat().st_mtime < MISSING_TOOLS_PERIOD_S:
            return ''
    except FileNotFoundError:
        pass
    stamp_path.touch()
    return text


def on_session_end(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Ask the server to tear down, and return at once.

    SessionEnd hooks share a 1.5 s budget that a plugin hook's `timeout` does not raise, so the
    hook only touches the shutdown file and the server disconnects on its next tick. `/clear`
    and `/resume` also fire SessionEnd while the same Claude process continues, so those
    reasons keep every connection.
    """
    if payload.get('reason') in KEEP_REASONS:
        return None
    from remote_ssh import state

    runtime = state.Runtime.current()
    if runtime is None:
        return None
    server_pid = runtime.owner().get('server_pid')
    if server_pid and is_pid_alive(server_pid):
        runtime.shutdown_path.touch()
    return None


def deny_redirect(reason: str) -> dict[str, Any] | None:
    """Return the PreToolUse output that blocks a Bash call the redirect cannot carry."""
    return build_output('PreToolUse', permissionDecision='deny', permissionDecisionReason=deny_text(reason))


def deny_text(reason: str, *extra: tuple[str, str]) -> str:
    """Return the deny reason shown for a redirected Bash call: `reason:`, extra fields, then `next:`."""
    from remote_ssh import fmt

    return fmt.error('redirect', None, reason, 'retry, prefix the command with local: to run it here, or call remote_mode(null)', *extra)


def bash_deny_rules(cwd: str) -> list[tuple[str, str]]:
    """Return the `Bash(<pattern>)` deny rules of the user and project settings.

    Reads `permissions.deny` of `<config dir>/settings.json` (`CLAUDE_CONFIG_DIR`, else
    `~/.claude`), `<cwd>/.claude/settings.json` and `<cwd>/.claude/settings.local.json`.

    Args:
        cwd: Project directory of the session.

    Returns:
        `(pattern, settings file)` pairs. A bare `Bash` rule is not listed: it removes the tool,
        so this hook never runs.

    Raises:
        OSError: A settings file exists but cannot be read.
        ValueError: A settings file exists but is not a JSON object.
    """
    config_dir = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.path.expanduser('~'), '.claude')
    files = [os.path.join(config_dir, 'settings.json')]
    if cwd:
        files += [os.path.join(cwd, '.claude', name) for name in ('settings.json', 'settings.local.json')]
    rules = []
    for path in files:
        try:
            with open(path, encoding='utf-8') as handle:
                settings = json.load(handle)
        except FileNotFoundError:
            continue
        if not isinstance(settings, dict):
            raise ValueError(f'{path} is not a JSON object')
        permissions = settings.get('permissions')
        deny = permissions.get('deny') if isinstance(permissions, dict) else None
        for rule in deny if isinstance(deny, list) else []:
            if isinstance(rule, str) and rule.startswith('Bash(') and rule.endswith(')'):
                rules.append((rule[5:-1], path))
    return rules


def command_segments(command: str) -> list[str]:
    """Split a shell command into the simple commands a deny rule must be checked against.

    Splits at `; && || | |& &`, newlines, backticks, parentheses and braces, whether or not
    they are quoted, so a doubtful parse yields extra segments and never hides one. Each segment
    is returned as written and again without leading keywords, wrappers (`timeout 5`, `nohup`,
    `FOO=1`), quotes and backslashes, and with its program reduced to a basename.

    Args:
        command: Bash command as Claude wrote it.

    Returns:
        The whole command plus every segment variant, deduplicated, in order.
    """
    found = {command.strip(): None}
    for raw in SEGMENT_SPLIT.split(command):
        segment = ' '.join(raw.split())
        if not segment:
            continue
        found[segment] = None
        words = re.sub(r'[\'"\\]', '', segment).split()
        while words and (words[0] in LEADING_WORDS or ASSIGNMENT.match(words[0]) or re.fullmatch(r'-?\d+[smhd]?', words[0]) or words[0].startswith('-')):
            words.pop(0)
        if words:
            words[0] = words[0].rsplit('/', 1)[-1] or words[0]
            found[' '.join(words)] = None
    return list(found)


def rule_matches(pattern: str, segment: str) -> bool:
    """Return whether a Bash permission pattern matches one command.

    Follows the permission rule syntax: no `*` is an exact match, `*` matches any text, a
    trailing ` *` or `:*` also matches the bare command, and the space before it is part of the
    rule.

    Args:
        pattern: Text between `Bash(` and `)`.
        segment: Command to test.
    """
    if pattern.endswith(':*'):
        pattern = pattern[:-2] + ' *'
    if pattern == '*':
        return True
    if pattern.endswith(' *') and pattern.count('*') == 1:
        head = pattern[:-2]
        return segment == head or segment.startswith(head + ' ')
    return re.fullmatch('.*'.join(re.escape(part) for part in pattern.split('*')), segment, re.DOTALL) is not None


def matching_deny_rule(command: str, cwd: str) -> tuple[str, str] | None:
    """Return the first `(pattern, settings file)` deny rule that matches any part of a command."""
    rules = bash_deny_rules(cwd)
    if not rules:
        return None
    segments = command_segments(command)
    for pattern, path in rules:
        if any(rule_matches(pattern, segment) for segment in segments):
            return pattern, path
    return None


def bash_timeout_s(tool_input: dict[str, Any]) -> int:
    """Return the remote time limit in seconds for a Bash call.

    Args:
        tool_input: Bash tool input, whose `timeout` is in milliseconds.

    Returns:
        The timeout rounded up to whole seconds and capped at `MAX_BASH_TIMEOUT_S`, or
        `DEFAULT_BASH_TIMEOUT_S` when absent or not a positive number.
    """
    value = tool_input.get('timeout')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return DEFAULT_BASH_TIMEOUT_S
    return min(MAX_BASH_TIMEOUT_S, max(1, -(-int(value) // 1000)))


def on_pre_tool_use(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Rewrite a Bash call into an ssh call on the redirect target.

    Fails closed: while a redirect target is set, a Bash call that cannot reach it is denied,
    never run locally. That covers a target that is not `connected`, an unreadable state.json,
    a redirect flag whose runtime cannot be found and any error while building the rewrite. A
    `local:` prefix always runs locally.

    Claude Code matches permission rules against the rewritten ssh command, so the user's Bash
    deny rules would never match the original. The hook therefore checks them against the
    original command and denies on a match, and it shows the original command in the approval
    prompt. The remote command first prints `[<alias>]` on its own line, so a redirected result
    shows which host ran it, and the command's exit code passes through unchanged.
    """
    if payload.get('tool_name') != 'Bash':
        return None
    import shlex

    from remote_ssh import state
    from remote_ssh.conn import exec_argv

    runtime = state.Runtime.current()
    if runtime is None:
        if any(state.runtime_base().glob('*/redirect-on')):
            return deny_redirect('redirect is on but the owning Claude process was not found')
        return None
    try:
        saved = runtime.load_state(strict=True)
    except (OSError, ValueError) as exc:
        if runtime.redirect_flag_path.exists():
            return deny_redirect(f'redirect state unreadable ({exc})')
        return None
    target = saved.get('redirect')
    if not target:
        return None
    tool_input = payload.get('tool_input') or {}
    command = tool_input.get('command', '')
    stripped = command.lstrip()
    if stripped.startswith(LOCAL_PREFIX):
        return build_output('PreToolUse', updatedInput={**tool_input, 'command': stripped[len(LOCAL_PREFIX):].lstrip()})
    entry = saved['hosts'].get(target) if isinstance(saved.get('hosts'), dict) else None
    status = entry.get('status') if isinstance(entry, dict) else None
    if status != 'connected':
        return deny_redirect(f'{target} is {status or "not connected"}')
    try:
        matched = matching_deny_rule(command, payload.get('cwd') or '')
    except (OSError, ValueError) as exc:
        return deny_redirect(f'cannot read the Bash deny rules ({exc})')
    if matched is not None:
        return build_output('PreToolUse', permissionDecision='deny', permissionDecisionReason=deny_text(
            f'permission rule Bash({matched[0]}) from {matched[1]} matches, and Claude Code cannot apply it to the redirected command',
            ('command', command[:REASON_COMMAND_CHARS])))
    try:
        extra = saved['ssh_opts'] if 'ssh_opts' in saved else shlex.split(os.environ.get('REMOTE_SSH_SSH_OPTS', ''))
        marked = f"{{ printf '[%s]\\n' {shlex.quote(target)}\n{command}\n}}"
        argv = exec_argv(runtime.ctl_path(target), entry['host'], marked, entry.get('path'), extra=extra or None,
                         remote_timeout=bash_timeout_s(tool_input))
    except Exception as exc:
        sys.stderr.write(f'remote-ssh: {exc!r}\n')
        return deny_redirect(f'cannot build the command for {target} ({exc})')
    updated_input = {**tool_input, 'command': shlex.join(argv)}
    if not is_option_enabled('REDIRECT_CONFIRM'):
        return build_output('PreToolUse', updatedInput=updated_input)
    shown = command if len(command) <= REASON_COMMAND_CHARS else command[:REASON_COMMAND_CHARS] + '...'
    return build_output('PreToolUse', permissionDecision='ask', permissionDecisionReason=f'redirect {target} · runs on {target}\ncommand: {shown}', updatedInput=updated_input)


def record_session(runtime: Any, payload: dict[str, Any]) -> None:
    """Write the session id of a hook payload into owner.json while the server waits for one.

    `/reload-plugins` starts a new server without firing SessionStart, so the server asks for the
    id with the `need-session` flag. The first message hook that carries an id answers it, and the
    server registers the session on its next tick. Does nothing without the flag.

    Args:
        runtime: Runtime directory of the owning Claude process.
        payload: Hook stdin JSON, whose `session_id` and `cwd` are recorded.
    """
    if not runtime.need_session_path.exists():
        return
    import socket

    from remote_ssh import relay

    session_id, cwd = payload.get('session_id'), payload.get('cwd') or os.getcwd()
    if session_id and not runtime.owner().get('session_id'):
        runtime.update_owner(session_id=session_id, cwd=cwd, name=relay.session_name(cwd, session_id), claude_pid=runtime.claude.pid,
                             claude_proc_start=runtime.claude.start, hostname=socket.gethostname())
    if runtime.owner().get('session_id'):
        runtime.need_session_path.unlink(missing_ok=True)


def on_message_check(payload: dict[str, Any], event: str) -> dict[str, Any] | None:
    """Inject queued host context blocks and undelivered messages (UserPromptSubmit, PostToolUse)."""
    from remote_ssh import inbox, relay, state

    runtime = state.Runtime.current()
    session_id = payload.get('session_id')
    if runtime is not None:
        record_session(runtime, payload)
    blocks = runtime.claim_context() if runtime is not None else []
    store = relay.Store()
    text = inbox.render_claimed(store, session_id) if session_id else ''
    if text:
        blocks.append(text)
    if runtime is not None and session_id and not store.has_pending(session_id):
        runtime.pending_path.unlink(missing_ok=True)
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

    Every event fails open except PreToolUse, whose Python runs only while a redirect flag
    exists: an error there denies the Bash call, so it never runs locally by accident.

    Args:
        event: Hook event name, e.g. `PreToolUse`.
    """
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        output = dispatch(event, payload if isinstance(payload, dict) else {})
    except Exception as exc:
        sys.stderr.write(f'remote-ssh hook {event}: {exc!r}\n')
        output = deny_redirect(f'hook failed ({exc!r})') if event == 'PreToolUse' else None
    if output is not None:
        sys.stdout.write(json.dumps(output) + '\n')
        sys.stdout.flush()
