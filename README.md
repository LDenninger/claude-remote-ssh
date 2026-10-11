<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.svg">
    <img src="assets/banner-light.svg" alt="claude-remote-ssh" width="480">
  </picture>
</h1>

<p align="center">
  <a href="https://github.com/LDenninger/claude-remote-ssh/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/LDenninger/claude-remote-ssh/ci.yml?branch=master&label=tests" alt="tests"></a>
  <a href="https://github.com/LDenninger/claude-remote-ssh/tags"><img src="https://img.shields.io/github/v/tag/LDenninger/claude-remote-ssh?label=version&sort=semver" alt="version"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"></a>
</p>

<p align="center">Let Claude Code sessions on different machines message each other, and work on remote hosts over SSH.</p>

Sessions on your laptop and your servers find each other and exchange messages, so one session can
hand work to another or ask a training run's session for its status. The same SSH connection runs
commands, mounts the remote project and loads its memory, and all of it ends with the session.

## ✨ Features

- **Messaging between machines:** sessions list each other, send messages and get replies in the
  conversation. All traffic runs over the local session's SSH connection, so servers never connect
  back.
- **Remote commands:** `remote_exec` runs one command. Redirect mode sends every `Bash` call to a host,
  and `local:` keeps a command local.
- **Remote memory:** the remote project's `MEMORY.md` index is loaded beside your local memory.
- **Persistent connections:** one SSH ControlMaster per host, reconnected with backoff. Several hosts
  connect at once.
- **Project mounts:** the remote project is mounted with sshfs, so Read, Edit and Write work on it.
- **Low overhead:** hooks start Python only when a message or redirect is pending.

## 📦 Install

```bash
claude plugin marketplace add LDenninger/claude-remote-ssh
claude plugin install remote-ssh@remote-ssh
```

For development:

```bash
git clone https://github.com/LDenninger/claude-remote-ssh.git
claude --plugin-dir ./claude-remote-ssh
```

### Dependencies

Local: OpenSSH 8.0+, Python 3.9+, and sshfs with FUSE for mounts and remote memory.

| OS | install command |
|---|---|
| Ubuntu / Debian | `sudo apt install openssh-client sshfs python3` |
| Fedora | `sudo dnf install openssh-clients fuse-sshfs python3` |
| macOS | install [macFUSE](https://macfuse.github.io/), then `brew install gromgit/fuse/sshfs-mac` |

Remote: `bash` and an SSH server, plus this plugin for messaging. Tested on Ubuntu 22.04, 24.04 and
26.04 (system Python 3.10, 3.12, 3.14) and macOS (Python 3.9, 3.12).

## 🚀 Usage

```text
/remote-ssh:remote-connect gpu-a
/remote-ssh:remote-connect gpu-b user@gpu-b.example.org ~/projects/train
/remote-ssh:remote-status
```

Arguments are `<alias> [host] [path]`, with `host` defaulting to the alias and `path` to the local
project path. After connecting, ask Claude in plain language to run, edit or message on a host.

### Tools

| tool | arguments | returns |
|---|---|---|
| `remote_connect` | alias, host, path | mounts, remote sessions, memory index |
| `remote_disconnect` | alias | teardown report |
| `remote_status` | none | hosts, mounts, redirect, missing tools |
| `remote_exec` | alias, command, cwd?, timeout_s? | stdout, stderr, exit code |
| `remote_mode` | alias or null | redirect target |
| `remote_sessions` | alias? | sessions on that host, or locally |
| `remote_send` | alias?, session, body | message id |
| `remote_inbox` | none | received messages |

### Messaging

```text
you        Ask the session on gpu-a whether the training run finished.
laptop     remote_sessions("gpu-a")          -> train-3f2a  ~/proj  busy
           remote_send("gpu-a", "train-3f2a", "Did the run finish? Report the final val loss.")
gpu-a      [remote-ssh] message from laptop/proj-91c0 ... -> checks the log, replies with remote_send
laptop     [remote-ssh] message from gpu-a/train-3f2a: Finished at epoch 40, val loss 0.212.
```

Messages arrive with the next prompt or tool call, and through a monitor in interactive sessions.

## ⚙️ Configuration

Hosts to connect at session start go in `<project>/.claude/remote-ssh.local.json`, kept out of git:

```json
{ "hosts": [ { "alias": "gpu-a", "host": "gpu-a", "path": "~/proj", "autoconnect": true } ] }
```

Plugin options, set through `/config`:

| option | default | effect |
|---|---|---|
| `messaging` | `true` | register on connected hosts and relay messages |
| `poll_interval_s` | `2` | health check and relay period in seconds |
| `sshfs_options` | empty | extra `-o` options for every sshfs mount |
| `redirect_confirm` | `true` | ask before each redirected `Bash` command |

## 🔧 Troubleshooting

- **`Permission denied` or host key error:** `ssh <host> true` must succeed without a prompt. Run
  `ssh-add` or accept the host key once by hand.
- **Mount hangs (`Transport endpoint is not connected`):** disconnect and reconnect the alias.
- **Stale mount after a crash:** `fusermount3 -uz <mount path>` on Linux, `diskutil unmount force
  <mount path>` on macOS. `/remote-ssh:remote-status` prints the paths.
- **No mounts on macOS:** allow the macFUSE extension under System Settings, Privacy & Security.

## 🔒 Security

- Credentials come only from `~/.ssh/config` and the SSH agent. The plugin never stores or asks for
  them.
- `remote_exec` and redirected `Bash` calls go through Claude Code's permission prompts.
- Only `.claude/remote-ssh.local.json` starts connections. A committed `.claude/remote-ssh.json` is
  ignored.
- Messages and remote memory are treated as data, never instructions. Redirect denies `Bash` calls
  while its host is down instead of running them locally.

## 🤝 Contributing

Questions and bug reports go to [GitHub issues](https://github.com/LDenninger/claude-remote-ssh/issues),
and pull requests are welcome. CI validates the plugin and runs the tests on Ubuntu 22.04, 24.04 and
26.04 and on macOS. See [CONTRIBUTING.md](CONTRIBUTING.md).

### Testing

```bash
python3 -m pytest tests/unit -q
python3 -m pytest tests/integration -q   # needs sshd and sshfs
claude plugin validate . --strict
```

## 📄 License

[MIT](LICENSE) (`MIT`) © 2026 Luis Denninger
