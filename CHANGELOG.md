# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] - 2026-10-14

### Added

- `remote_send` takes `files`, up to 10 local files of 5 MB in total, copied over the existing
  connection to `attachments/<message id>/` in the recipient's message store and listed in the
  delivered message under `attachments:`. A recipient that pulls from a host outbox receives them
  with the message.
- `remote_inbox` lists the messages this session sent with a state: `waiting`, `read` or `answered`.
  A host's states cost one ssh round trip per call.
- A session keeps its name across `--resume` and `/clear` in the same directory. The new registration
  takes over the name, moves the unread inbox and carries `previous_ids`, so messages to an old id or
  name reach it, on the host too.
- A server that has no session id (plugin loaded with `/reload-plugins`) raises a `need-session`
  flag, and the next prompt or tool-call hook writes the id from its payload, so the session registers.

### Changed

- `remote_connect` counts `N other sessions`, the rows `remote_sessions` shows without `this session`.
  Where Bash runs moves from the first line (`bash local`) to a `bash:` field that names the
  command to switch (`runs here (remote_mode("node2") runs it on node2)`).
- The `memory:` field of `remote_connect` reads `none` when the remote project has no memory
  directory or an empty index, and `not mounted` when the mount is unavailable.
- `remote_status` adds one `--- mounts of <alias> ---` block per host with the remote path and its
  local mount for files and memory, and `next: remote_connect(...)` when no host is connected.
- `remote_disconnect` reports `replies fetched` only when a reply was fetched, and its note names the
  memory path that is gone and how to read it again.
- A redirected Bash result starts with the line `[<alias>]`, so the host that ran it is visible. The
  exit code is unchanged.
- `remote_mode` also reads the strings `null`, `none` and `off` as local mode. Any other alias must be
  connected.
- `remote_send` reports `delivered` and that the reply arrives in this conversation, without the
  message id and route. An unknown target lists the current session names and why it may be missing.
- `remote_sessions` marks the caller's own registration `(this session)` and names the machine it runs on.

### Fixed

- A message of about 1 800 characters arrived cut off mid-word while `remote_send` reported success.
  Claude Code 2.1.296 cuts a monitor line at 500 characters and a monitor event at 3 000, and hook
  context at 8 000 characters and 200 lines. The monitor wraps long lines and prints a one-line
  notice for a message above 2 800 characters, and the hook injects a notice above 7 440 characters
  or 190 lines. `remote_inbox` shows the full text.
- Runtime directories whose `owner.json` has no `claude_pid` were never removed. They are removed
  when their server process is dead.

## [0.1.1] - 2026-10-11

### Fixed

- The MCP server is declared in `.claude-plugin/plugin.json` instead of a root `.mcp.json`, so a
  session started inside the plugin repository no longer loads it as a broken project server.
- One slow or failed health probe no longer tears down a working SSH connection: the server
  reconnects a host after 3 failed probes in a row, or at once when the master process exited.
  `remote_status` reports `master-down` from a local socket probe instead of an `ssh -O check`.

### Changed

- Every tool result and injected message follows one fixed output format: a first line `<tool> <host> · <facts>`,
  `key: value` lines, `--- name ---` blocks, and `reason:` plus `next:` on errors.
- The MCP server is named `ssh`, so the chat shows `plugin:remote-ssh:ssh` instead of
  `plugin:remote-ssh:remote-ssh`. Saved permission rules for the old tool names
  (`mcp__plugin_remote-ssh_remote-ssh__*`) need to be renamed to `mcp__plugin_remote-ssh_ssh__*`.

## [0.1.0] - 2026-10-11

### Added

- MCP server with the tools `remote_connect`, `remote_disconnect`, `remote_status`, `remote_exec`,
  `remote_mode`, `remote_sessions`, `remote_send` and `remote_inbox`.
- One SSH ControlMaster connection per host alias, with health checks and reconnect backoff.
- sshfs mounts of the remote project directory and the remote project memory, removed at session
  end.
- Redirect mode: a `PreToolUse` hook rewrites `Bash` calls to run on the redirect target, with a
  `local:` prefix for local commands.
- Remote memory index injected at session start, in `remote_connect` results, and with the next
  prompt or tool call after a background connect. Its links stay relative, and the block names the
  memory mount they resolve against once.
- Messaging between Claude Code sessions on different machines through a plugin-owned inbox and
  outbox, delivered by a monitor and by hooks.
- Per-project autoconnect through `~/.claude/remote-ssh/autoconnect.json`, keyed by the absolute
  project path. The file lives outside the repository, so a repository can never start a
  connection. `.claude/remote-ssh.json` and `.claude/remote-ssh.local.json` are ignored, with a note
  at session start.
- Hook commands that test for pending work in shell and start Python only when there is some.
- Redirect mode fails closed: a `Bash` call is denied while its host is not connected.
- Relayed messages carry the host alias they were pulled from.
- Remote memory follows Claude Code's per-repository memory directory, named after the git root.
- Skills `remote-ssh` (model-invoked), `/remote-ssh-connect` and `/remote-ssh-status`.
- `PRIVACY.md`, and README sections on what the plugin runs, sends and stores.

### Security

- Redirect applies the user's `Bash` deny rules to the original command and denies matches. The
  approval prompt shows the original command.
- Header fields of messages from remote hosts are sanitized and quoted, bodies are capped at 8 000
  characters, and messages reach the model as quoted data that names the relaying host.
- CI actions are pinned to commit SHAs, and the Claude Code CLI and pytest versions are pinned.
