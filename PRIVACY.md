# Privacy Policy

remote-ssh has no telemetry and no analytics. It sends data nowhere except the SSH hosts you connect
to, over your own SSH configuration. The maintainer receives nothing.

## What is written locally

- `$XDG_RUNTIME_DIR/remote-ssh/` (`$TMPDIR/rssh` on macOS, `/tmp/remote-ssh-<uid>` as fallback):
  connection state, `server.log`, SSH sockets and sshfs mountpoints.
- `~/.claude/remote-ssh/`: `machine-id.<hostname>`, `sessions/`, `inbox/`, `outbox/` and `autoconnect.json`.

## What is written on connected hosts

- `~/.claude/remote-ssh/sessions/<sid>.json`: local hostname, working directory, Claude process id,
  session id, session name and machine id. Other sessions on that host can read it.
- `~/.claude/remote-ssh/inbox/` and `outbox/`: messages between sessions, including their bodies.
- The remote `~/.claude/projects/<slug>/memory`, when Claude saves a remote memory.

Setting the `messaging` option to false stops session registration and message relay.

## Retention

Local inbox and outbox entries are swept after 7 days, and `read/` keeps at most 200 messages. Other
files stay until you delete them. Uninstalling the plugin removes none of the directories above.

## Deleting your data

```bash
rm -rf ~/.claude/remote-ssh                  # local
ssh <host> 'rm -rf ~/.claude/remote-ssh'     # each host
```

The runtime directory is removed at reboot on Linux, or with `rm -rf` on its path.

## Contact

Questions go to [GitHub issues](https://github.com/LDenninger/claude-remote-ssh/issues), or to
luis.denninger@gmail.com. Security reports follow [SECURITY.md](SECURITY.md).
