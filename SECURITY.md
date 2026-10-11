# Security Policy

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub security advisories](https://github.com/LDenninger/claude-remote-ssh/security/advisories/new),
never in a public issue. Expect a first reply within 7 days.

## Scope

remote-ssh runs commands and mounts files on machines you can already reach over SSH. Reports are in
scope when the plugin lets something cross a boundary it should not:

- another local user reading or writing the runtime directory (`$XDG_RUNTIME_DIR/remote-ssh`, `$TMPDIR/rssh` on macOS,
  `/tmp/remote-ssh-<uid>` as fallback) or the message store under `~/.claude/remote-ssh/`
- a remote command or message body executing locally
- prompt injection through remote content: messages, a remote `MEMORY.md` or session listings
  reaching the model unsanitized, unquoted, uncapped or without naming the relaying host
- a repository starting a connection, or its files changing what a redirected command runs
- a redirected `Bash` command or `remote_exec` call bypassing the Claude Code permission flow
- a redirected command escaping a `Bash` deny rule that matches the original command
- credentials or session data leaving the machine they belong to

Text that a process running as your user on a connected host places in your session's context is a
known limit. The plugin quotes, sanitizes and caps it, which reduces the risk without removing it.
Access that your SSH configuration already grants, such as a compromised remote account, is out of
scope.

## Supported versions

Security fixes go into the latest release only.
