# claude-remote-ssh

[![CI](https://github.com/LDenninger/remote-ssh/actions/workflows/ci.yml/badge.svg)](https://github.com/LDenninger/remote-ssh/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A Claude Code plugin that connects one session to remote machines over SSH for the session's lifetime.

Connect a host by alias, and the session runs commands on it, edits its project files through an
sshfs mount, reads and writes its project memory, and exchanges messages with Claude Code sessions
running there. Every connection, mount and registration ends with the session.

## ✨ Features

- **Persistent connections:** one SSH ControlMaster per host, health-checked and reconnected with
  backoff. Several hosts connect at once, and a failing host never blocks another.
- **Remote commands:** `remote_exec` runs one command on a host. Redirect mode sends every `Bash`
  call to one host, and a `local:` prefix keeps a command on the local machine.
- **Project mounts:** the remote project directory is mounted with sshfs, so Read, Edit and Write
  work on remote files.
- **Remote memory:** the remote project's `MEMORY.md` index is loaded into the session, with links
  pointing at a mount of the remote memory directory.
- **Messaging:** sessions on different machines send each other messages through the plugin's own
  inbox. Every transfer runs over the local session's connection, so the remote host never connects
  back.
- **Autoconnect:** hosts listed in `.claude/remote-ssh.json` connect in the background at session
  start.

## 🔒 Security

- Every plugin directory is mode 0700, and its owner and mode are checked before each use.
- The plugin never stores or prompts for credentials. Authentication comes from `~/.ssh/config` and
  the SSH agent, and `BatchMode=yes` makes a login prompt fail at once.
- `remote_exec` goes through the MCP tool permission prompt. A redirected `Bash` call asks for
  approval of the rewritten `ssh` command line while `redirect_confirm` is on.
- Only processes running as your user read or write your inboxes. Message bodies reach the session
  as quoted text with the sender named, and the bundled `remote` skill tells Claude to treat them
  as information, never as instructions.

## 📦 Install

```bash
claude plugin marketplace add LDenninger/remote-ssh
claude plugin install remote-ssh@remote-ssh
```

To load a local checkout for development:

```bash
git clone https://github.com/LDenninger/remote-ssh.git
claude --plugin-dir ./remote-ssh
```

### Dependencies

On the local machine: OpenSSH 8.0 or later, Python 3.9 or later, and sshfs with FUSE for the mounts
and remote memory.

| OS | install command |
|---|---|
| Ubuntu / Debian | `sudo apt install openssh-client sshfs python3` |
| Fedora | `sudo dnf install openssh-clients fuse-sshfs python3` |
| macOS | install [macFUSE](https://macfuse.github.io/), then `brew install gromgit/fuse/sshfs-mac` |

On each remote host: `bash` and an SSH server. Messaging with a remote session also needs Claude
Code with this plugin installed on that host. Without sshfs or FUSE, mounts and remote memory are
off and every other feature works, and the session start shows the missing tool with its install
command.

## 🚀 Usage

Connect a host from inside a Claude Code session:

```text
/remote-ssh:remote-connect gpu-a
/remote-ssh:remote-connect gpu-b user@gpu-b.example.org ~/projects/train
```

The arguments are `<alias> [host] [path]`. `host` defaults to the alias, and `path` defaults to the
local project path. Aliases use 1 to 16 characters from `a-z`, `0-9` and `-`.

`/remote-ssh:remote-status` lists every host with its connection state, mounts and the redirect
target. After connecting, ask Claude in plain language to run, edit or message on a host.

### Tools

| tool | arguments | returns |
|---|---|---|
| `remote_connect` | alias, host, path | mounts, reachable remote sessions, remote memory index |
| `remote_disconnect` | alias | teardown report |
| `remote_status` | none | every host: connection, mounts, redirect, missing tools |
| `remote_exec` | alias, command, cwd?, timeout_s? | stdout, stderr, exit code |
| `remote_mode` | alias or null | new redirect target |
| `remote_sessions` | alias? | sessions registered on that host, or locally |
| `remote_send` | alias?, session, body | message id and target |
| `remote_inbox` | none | delivered and pending messages |

### Messaging

Install the plugin on both machines. Connecting to a host registers the local session there, and
`remote_sessions(alias)` lists the sessions a message can reach. A remote session replies with
`remote_send` and no alias, and the local session pulls the reply over its connection every
`poll_interval_s` seconds.

A message reaches an idle recipient through the plugin's monitor within about two poll intervals.
Monitors run only in interactive sessions, so a session without one sees the message at its next
prompt or tool call.

## ⚙️ Configuration

A project lists hosts to connect at session start in `<project>/.claude/remote-ssh.json`:

```json
{ "hosts": [ { "alias": "gpu-a", "host": "gpu-a", "path": "~/proj", "autoconnect": true } ] }
```

Plugin options are set when the plugin is enabled, and later through `/config`:

| option | default | effect |
|---|---|---|
| `messaging` | `true` | register this session on connected hosts and relay messages |
| `poll_interval_s` | `2` | health check and message relay period in seconds, 1 to 60 |
| `sshfs_options` | empty | extra options appended with `-o` to every sshfs mount |
| `redirect_confirm` | `true` | ask for approval of each redirected `Bash` command |

## 🔧 Troubleshooting

- **Connect fails with `Permission denied` or a host key error:** run `ssh <host> true` in a
  terminal. It must succeed without any prompt, since the plugin connects with `BatchMode=yes`. Load
  the key into the agent with `ssh-add`, or accept the host key once by hand.
- **A mount hangs or shows `Transport endpoint is not connected`:** the host lost its connection.
  Run `/remote-ssh:remote-status`, then `remote_disconnect` and `remote_connect` for that alias.
- **A stale mount survives a crash:** on Linux run `fusermount3 -uz /tmp/remote-ssh-$(id -u)/*/<alias>/fs`,
  on macOS `diskutil unmount force <mount path>`. The next session start also removes leftovers of
  dead sessions.
- **No mounts on macOS:** allow the macFUSE system extension under System Settings, Privacy &
  Security, and reboot.

## 🤝 Contributing

Questions and bug reports go to [GitHub issues](https://github.com/LDenninger/remote-ssh/issues),
and pull requests are welcome. CI runs `claude plugin validate . --strict`, the unit tests on Ubuntu
and macOS with Python 3.9 and 3.12, and the integration tests against a local sshd on Ubuntu.

### Testing

```bash
python3 -m pip install pytest
python3 -m pytest tests/unit -q
python3 -m pytest tests/integration -q   # needs sshd and sshfs
claude plugin validate . --strict
claude --plugin-dir .                    # load the checkout in a session
```

## 📄 License

[MIT](LICENSE) (`MIT`) © 2026 Luis Denninger
