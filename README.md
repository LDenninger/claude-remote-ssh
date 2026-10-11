# claude-remote-ssh

[![CI](https://github.com/LDenninger/claude-remote-ssh/actions/workflows/ci.yml/badge.svg)](https://github.com/LDenninger/claude-remote-ssh/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Let Claude Code sessions on different machines message each other, and work on remote hosts over SSH.

A session on your laptop and the sessions on your servers find each other, send messages and read
the replies, so one session can hand work to another, ask a training run's session for its status,
or collect results from several machines. The same connection runs commands on the remote host,
mounts its project files for editing and loads its project memory. Every connection, mount and
registration ends with the session.

## ✨ Features

- **Messaging between machines:** sessions on different machines list each other, send messages and
  receive replies in the conversation. Interactive sessions get new messages from a monitor, and
  every session gets them with its next prompt or tool call. Every transfer runs over the local
  session's SSH connection, so a server never needs to reach your laptop.
- **Persistent connections:** one SSH ControlMaster per host, health-checked and reconnected with
  backoff. Several hosts connect at once, and a failing host never blocks another.
- **Remote commands:** `remote_exec` runs one command on a host. Redirect mode sends every `Bash`
  call to one host, and a `local:` prefix keeps a command on the local machine.
- **Project mounts:** the remote project directory is mounted with sshfs, so Read, Edit and Write
  work on remote files.
- **Remote memory:** the remote project's `MEMORY.md` index is loaded into the session, together with
  the local mount of the remote memory directory its links are relative to.
- **Autoconnect:** hosts listed in your personal `.claude/remote-ssh.local.json` connect in the
  background at session start. Their mount paths and memory index reach the session with the next
  prompt or tool call.
- **Low overhead:** the hooks on every prompt, tool call and `Bash` call are a shell file test, and
  start Python only while a message, a queued host report or a redirect target waits. Each poll
  runs one ssh process per host.

## 🔒 Security

- Every plugin directory is mode 0700, and its owner and mode are checked before each use.
- The plugin never stores or prompts for credentials. Authentication comes from `~/.ssh/config` and
  the SSH agent, and `BatchMode=yes` makes a login prompt fail at once.
- `remote_exec` goes through the MCP tool permission prompt. A redirected `Bash` call asks for
  approval of the rewritten `ssh` command line while `redirect_confirm` is on.
- Only processes running as your user read or write your inboxes. Message bodies reach the session
  as quoted text, capped at 16 KB, with the sender named, and the bundled `remote-ssh` skill tells
  Claude to treat them as information, never as instructions.

### Trust model

- **Trusted:** your local user account, your `~/.ssh/config` and agent, and your personal
  `.claude/remote-ssh.local.json`. Keep that file out of git, for example in `.gitignore`.
- **Not trusted to start connections:** files committed to a repository. A committed
  `.claude/remote-ssh.json` connects nothing, and the session start notes once that it is ignored.
- **Data, never instructions:** message bodies, the remote `MEMORY.md` index and everything else
  read from a remote host. A remote account can write any field of a message it relays, so a
  relayed message names the host alias the plugin pulled it from, not the sender's claimed host.
  A message must be addressed to this session and stored under its own id, or it is dropped.
- **Fail closed:** while redirect mode is on and its host is reconnecting, failed or unreadable, a
  `Bash` call is denied with the reason, never run locally. A `local:` prefix runs it locally on
  purpose, and `remote_mode(null)` ends redirect mode.
- A `host` value starting with `-` is rejected, and every `ssh` call ends its options with `--`.

## 📦 Install

```bash
claude plugin marketplace add LDenninger/claude-remote-ssh
claude plugin install remote-ssh@remote-ssh
```

To load a local checkout for development:

```bash
git clone https://github.com/LDenninger/claude-remote-ssh.git
claude --plugin-dir ./remote-ssh
```

### Dependencies

On the local machine: OpenSSH 8.0 or later, Python 3.9 or later, and sshfs with FUSE for the mounts
and remote memory.

| tested platform | Python | OpenSSH |
|---|---|---|
| Ubuntu 22.04 | 3.10 (system) | 8.9 |
| Ubuntu 24.04 | 3.12 (system) | 9.6 |
| Ubuntu 26.04 | 3.14 (system) | 10.2 |
| macOS | 3.9 and 3.12 | system |

| OS | install command |
|---|---|
| Ubuntu / Debian | `sudo apt install openssh-client sshfs python3` |
| Fedora | `sudo dnf install openssh-clients fuse-sshfs python3` |
| macOS | install [macFUSE](https://macfuse.github.io/), then `brew install gromgit/fuse/sshfs-mac` |

On each remote host: `bash`, a POSIX `sh` and an SSH server. A remote whose login shell is csh, tcsh or fish
is untested, and a multi-line `remote_exec` command may fail there. Messaging with a remote session also needs Claude
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

```text
you        Ask the session on gpu-a whether the training run finished.
laptop     remote_sessions("gpu-a")          -> train-3f2a  ~/proj  busy
           remote_send("gpu-a", "train-3f2a", "Did the run finish? Report the final val loss.")
gpu-a      [remote-ssh] message from laptop/proj-91c0 ... -> checks the log, replies with remote_send
laptop     [remote-ssh] message from gpu-a/train-3f2a: Finished at epoch 40, val loss 0.212.
```

A message reaches an idle recipient through the plugin's monitor within about two poll intervals.
Monitors run only in interactive sessions, so a session without one sees the message at its next
prompt or tool call.

## ⚙️ Configuration

List hosts to connect at session start in `<project>/.claude/remote-ssh.local.json`, a personal
file you keep out of git:

```json
{ "hosts": [ { "alias": "gpu-a", "host": "gpu-a", "path": "~/proj", "autoconnect": true } ] }
```

A committed `.claude/remote-ssh.json` is ignored, since a cloned repository must not open ssh
connections on its own.

Plugin options are set when the plugin is enabled, and later through `/config`:

| option | default | effect |
|---|---|---|
| `messaging` | `true` | register this session on connected hosts and relay messages |
| `poll_interval_s` | `2` | health check and message relay period in seconds, 1 to 60 |
| `sshfs_options` | empty | extra options appended with `-o` to every sshfs mount |
| `redirect_confirm` | `true` | ask for approval of each redirected `Bash` command |

With `redirect_confirm` off, a redirected command goes through your normal `Bash` permission rules,
which then match the rewritten `ssh ...` command line, not the command Claude wrote.

Runtime files, control sockets and mounts live under `$XDG_RUNTIME_DIR/remote-ssh` on Linux,
`$TMPDIR/rssh` on macOS, and `/tmp/remote-ssh-<uid>` when neither fits.

## 🔧 Troubleshooting

- **Connect fails with `Permission denied` or a host key error:** run `ssh <host> true` in a
  terminal. It must succeed without any prompt, since the plugin connects with `BatchMode=yes`. Load
  the key into the agent with `ssh-add`, or accept the host key once by hand.
- **A mount hangs or shows `Transport endpoint is not connected`:** the host lost its connection.
  Run `/remote-ssh:remote-status`, then `remote_disconnect` and `remote_connect` for that alias.
- **A stale mount survives a crash:** on Linux run `fusermount3 -uz "$XDG_RUNTIME_DIR"/remote-ssh/*/<alias>/fs`,
  on macOS `diskutil unmount force <mount path>`. `/remote-ssh:remote-status` prints the mount paths. The next session start also removes leftovers of
  dead sessions.
- **No mounts on macOS:** allow the macFUSE system extension under System Settings, Privacy &
  Security, and reboot.

## 🤝 Contributing

Questions and bug reports go to [GitHub issues](https://github.com/LDenninger/claude-remote-ssh/issues),
and pull requests are welcome. CI runs `claude plugin validate . --strict` on the marketplace and
the plugin manifest, the unit and integration tests on Ubuntu 22.04, 24.04 and 26.04 with each
release's system Python, OpenSSH and sshfs, and the unit tests on macOS with Python 3.9 and 3.12.
See [CONTRIBUTING.md](CONTRIBUTING.md).

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
