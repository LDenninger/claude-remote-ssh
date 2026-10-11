---
name: remote-ssh-connect
description: Connect this session to a remote host over SSH, mount its project directory and load its project memory.
argument-hint: <alias> [host] [path]
disable-model-invocation: true
---

# Connect a remote host

Arguments: `$ARGUMENTS`

1. Parse the arguments as `<alias> [host] [path]`.
   - `alias` is required. It names the host in this session and must match `[a-z0-9-]{1,16}`. If it
     is missing or invalid, ask the user for one and stop.
   - `host` defaults to `alias`. It is an `~/.ssh/config` host name or `user@hostname`.
   - `path` defaults to the current project path, the absolute path of the local working directory.
     A path starting with `~/` is resolved on the remote host.
2. Call the `remote_connect` tool with `alias`, `host` and `path`.
3. Report the result to the user: the connection status line, the mount paths, the remote sessions
   reachable for messaging, and whether remote memory was found.
4. If the tool returns an error, show its `reason:` and `next:` lines and any `detail from` block (ssh's stderr). For an authentication failure, tell the user to
   check that `ssh <host> true` succeeds without a prompt, since the plugin connects with
   `BatchMode=yes`.
