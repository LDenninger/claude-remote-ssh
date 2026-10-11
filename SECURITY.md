# Security Policy

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub security advisories](https://github.com/LDenninger/claude-remote-ssh/security/advisories/new),
never in a public issue. Expect a first reply within 7 days.

## Scope

remote-ssh runs commands and mounts files on machines you can already reach over SSH. Reports are in
scope when the plugin lets something cross a boundary it should not:

- another local user reading or writing the runtime directory under `/tmp/remote-ssh-<uid>/` or the
  message store under `~/.claude/remote-ssh/`
- a remote command or message body executing locally, or a message body being followed as an
  instruction because of how the plugin injects it
- a redirected `Bash` command or `remote_exec` call bypassing the Claude Code permission flow
- credentials or session data leaving the machine they belong to

Access that your SSH configuration already grants, such as a compromised remote account, is out of
scope.

## Supported versions

Security fixes go into the latest release only.
