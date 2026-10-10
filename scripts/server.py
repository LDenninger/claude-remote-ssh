#!/usr/bin/env python3
"""Entry point of the remote-ssh MCP stdio server."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from remote_ssh.server import main  # noqa: E402

if __name__ == "__main__":
    main()
