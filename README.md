<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.svg">
    <img src="assets/banner-light.svg" alt="claude-remote-ssh" width="500">
  </picture>
</h1>

<p>
  <a href="https://github.com/LDenninger/claude-remote-ssh/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/LDenninger/claude-remote-ssh/ci.yml?branch=master&label=tests" alt="tests"></a>
  <a href="https://github.com/LDenninger/claude-remote-ssh/tags"><img src="https://img.shields.io/github/v/tag/LDenninger/claude-remote-ssh?label=version&sort=semver" alt="version"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"></a>
</p>

<p>Let Claude Code sessions on different machines message each other, and work on remote hosts over SSH.</p>

Your laptop session sees only your laptop, and the session beside your training run sees only the
GPU server. remote-ssh links them over your existing SSH setup, so one session can ask another for
its status, hand it a task, or collect results from several servers. The same connection runs
commands, edits files and loads project memory on each host, and all of it closes with the session.

```text
# laptop: you type
Ask the training session on gpu whether the run finished.

# laptop: Claude finds that session and sends the question
remote_send("gpu", "run-3f2a", "Did the run finish? Report the final val loss.")

# laptop: the reply from gpu lands in your conversation
[remote-ssh] message from run-3f2a (3f2a91c0) relayed by host gpu at 2026-10-11T09:14:02+00:00:
> Finished at epoch 40, val loss 0.212.
```

## ✨ Features

- **Messaging between machines:** your session lists the Claude sessions on every connected host,
  sends them questions or tasks and gets their replies in the conversation. Traffic runs over your
  SSH connection, so servers never connect back. Both machines need the plugin.
- **Remote commands:** `remote_exec` runs one command on a host. Redirect mode sends every `Bash`
  call there, and a `local:` prefix keeps a single command on your machine.
- **Remote memory:** the remote project's `MEMORY.md` index loads beside your local memory.
- **Project mounts:** sshfs mounts the remote project, so Read, Edit and Write work on remote files.
- **Persistent connections:** one SSH ControlMaster per host, reconnected with backoff. Connect
  several hosts at once.
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

Remote: `bash` and an SSH server, plus this plugin for messaging. Linux and macOS are supported,
Windows is not. Unit and integration tests run on Ubuntu 22.04, 24.04 and 26.04 (Python 3.10, 3.12,
3.14), and macOS runs the unit tests only (Python 3.9, 3.12).

## 🚀 Usage

```text
/remote-ssh:remote-connect gpu-a
/remote-ssh:remote-connect gpu-b user@gpu-b.example.org ~/projects/train
/remote-ssh:remote-status
```

Arguments are `<alias> [host] [path]`, with `host` defaulting to the alias and `path` to the local
project path. Then ask in plain language:

```text
Ask the session on gpu-a to rerun the eval on the new checkpoint and report the score.
Collect the final val loss from the training sessions on gpu-a and gpu-b into a table.
Run nvidia-smi on gpu-b.
Send Bash to gpu-a and run the tests there.
```

A message reaches the other session through a monitor in interactive sessions, otherwise with that
session's next prompt or tool call.

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

## ⚙️ Configuration

Hosts to connect at session start go in `~/.claude/remote-ssh/autoconnect.json`, keyed by the
absolute project path. The file lives outside the repository, so a repository can never start a
connection. `.claude/remote-ssh.json` and `.claude/remote-ssh.local.json` are ignored.

```json
{ "projects": { "/home/me/proj": {
    "hosts": [ { "alias": "gpu-a", "host": "gpu-a", "path": "~/proj" } ] } } }
```

Plugin options, set through `/config`:

| option | default | effect |
|---|---|---|
| `messaging` | `true` | register on connected hosts and relay messages |
| `poll_interval_s` | `2` | health check and relay period in seconds |
| `sshfs_options` | empty | extra `-o` options for every sshfs mount |
| `redirect_confirm` | `true` | ask before each redirected `Bash` command, see [Security](#-security) |

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
- `remote_exec` is one MCP permission: choosing "don't ask again" approves every later remote
  command. Redirected `Bash` calls go through the `Bash` permission flow.
- Redirect applies your `Bash` deny rules to the original command and denies matches, and the
  approval prompt shows the original command. Allow rules are not applied to the original. With
  `redirect_confirm` off, whatever matches the rewritten `ssh ...` line approves the command, so do
  not allow `Bash(ssh:*)` in that mode.
- Redirect denies `Bash` calls while its host is down instead of running them locally.
- Message headers from remote hosts are sanitized and quoted, bodies are capped at 8 000 characters,
  and messages reach the model as quoted data that names the relaying host. This is a mitigation, not
  a guarantee: any process running as your user on a connected host, including other Claude sessions,
  can put text into your session's context. Turn it off with the `messaging` option.
- Remote memory is read as data, with the same limit.

### What it runs and stores

- **Hooks:** `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PostToolUse` (every tool call) and
  `PreToolUse` (every `Bash` call). They start Python only when work is pending.
- **Background processes:** the MCP server for the session, one SSH ControlMaster per host, one ssh
  round trip per host every `poll_interval_s`, and a monitor in interactive sessions.
- **Local files:** `$XDG_RUNTIME_DIR/remote-ssh/` (`$TMPDIR/rssh` on macOS, `/tmp/remote-ssh-<uid>`
  as fallback) holds state, `server.log`, sockets and mountpoints. `~/.claude/remote-ssh/` holds
  `machine-id.<hostname>`, `sessions/`, `inbox/`, `outbox/` and `autoconnect.json`.
- **On each connected host:** `~/.claude/remote-ssh/sessions/<sid>.json` with the local hostname, working
  directory, Claude pid, session id, session name and machine id, plus messages in `inbox/` and
  `outbox/`.
- **Remote memory:** read from and written to the remote `~/.claude/projects/<slug>/memory`.
- **Commands:** every command runs in `bash -lc` on the remote host.
- **Retention:** local inbox and outbox entries are swept after 7 days, and `read/` keeps 200
  messages. Uninstalling the plugin removes neither directory:

```bash
rm -rf ~/.claude/remote-ssh                  # local
ssh <host> 'rm -rf ~/.claude/remote-ssh'     # each host
```

Nothing is sent anywhere except the hosts you connect. See [SECURITY.md](SECURITY.md) and
[PRIVACY.md](PRIVACY.md).

## 🤝 Contributing

Questions and bug reports go to [GitHub issues](https://github.com/LDenninger/claude-remote-ssh/issues),
and pull requests are welcome. CI validates the plugin and runs the tests on Ubuntu 22.04, 24.04 and
26.04 and unit tests on macOS. See [CONTRIBUTING.md](CONTRIBUTING.md).

### Testing

```bash
python3 -m pytest tests/unit -q
python3 -m pytest tests/integration -q   # needs sshd and sshfs
claude plugin validate . --strict
```

## 📄 License

[MIT](LICENSE) (`MIT`) © 2026 Luis Denninger
