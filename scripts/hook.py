#!/usr/bin/env python3
"""Entry point of every remote-ssh hook. The first argument names the hook event."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from remote_ssh.hooks import main  # noqa: E402

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
