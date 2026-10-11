#!/usr/bin/env python3
"""Entry point of the remote-ssh inbox monitor."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from remote_ssh.inbox import watch_main  # noqa: E402

if __name__ == "__main__":
    watch_main()
