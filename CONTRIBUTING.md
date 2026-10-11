# Contributing

Bug reports, fixes and small features are welcome. Open an issue before a large change so the design
can be agreed first.

## Setup

```bash
git clone https://github.com/LDenninger/claude-remote-ssh.git
cd claude-remote-ssh
claude --plugin-dir .          # load the working copy into a Claude Code session
```

Runtime code uses the Python 3.9+ standard library only. Tests need `pytest`. The integration tests
also need `sshd`, `sshfs` and FUSE on Linux. They start their own unprivileged `sshd` on 127.0.0.1
and never touch your SSH config.

## Checks before a pull request

```bash
python3 -m pytest -q                    # unit tests
python3 -m pytest tests/integration -q  # integration tests (Linux)
claude plugin validate . --strict
```

## Guidelines

- Keep the hooks fast. They run on every tool call, so the common case must not start Python.
- Keep the token footprint small. Tool and skill descriptions are loaded into every session.
- Add a test that fails without your change.
- Write commit messages in the imperative mood, one concern per commit.
