# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

### Added

- MCP server with the tools `remote_connect`, `remote_disconnect`, `remote_status`, `remote_exec`,
  `remote_mode`, `remote_sessions`, `remote_send` and `remote_inbox`.
- One SSH ControlMaster connection per host alias, with health checks and reconnect backoff.
- sshfs mounts of the remote project directory and the remote project memory, removed at session
  end.
- Redirect mode: a `PreToolUse` hook rewrites `Bash` calls to run on the redirect target, with a
  `local:` prefix for local commands.
- Remote memory index injected at session start, in `remote_connect` results, and with the next
  prompt or tool call after a background connect.
- Messaging between Claude Code sessions on different machines through a plugin-owned inbox and
  outbox, delivered by a monitor and by hooks.
- Per-project autoconnect through `.claude/remote-ssh.json`.
- Hook commands that test for pending work in shell and start Python only when there is some.
- Skills `remote`, `remote-connect` and `remote-status`.
