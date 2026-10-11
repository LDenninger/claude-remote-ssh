---
name: remote-ssh-status
description: Show every connected remote host with its connection state, mounts, the redirect target and missing local tools.
disable-model-invocation: true
---

# Remote status

1. Call the `remote_status` tool.
2. Show the result's host table as it is: one row per host with its connection state, project
   mount, memory mount and other sessions.
3. Below the table, name the redirect target or state that redirect mode is off, and list any
   missing local tool with its install command from the result.
4. For a host in `failed` or `reconnecting` state, include its `error from <alias>` block.
