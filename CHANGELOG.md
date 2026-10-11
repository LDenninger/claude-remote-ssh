# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

## [0.1.1] - 2026-10-11

### Fixed

- The MCP server is declared in `.claude-plugin/plugin.json` instead of a root `.mcp.json`, so a
  session started inside the plugin repository no longer loads it as a broken project server.

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
