"""Check that every version string agrees, and print the changelog notes for a release.

Usage:
    python .github/scripts/release_check.py                 # versions agree
    python .github/scripts/release_check.py --tag v0.1.0    # tag matches and its changelog section is dated
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def versions() -> dict[str, str]:
    """Return the version declared by each file that carries one."""
    return {
        '.claude-plugin/plugin.json': json.loads((ROOT / '.claude-plugin/plugin.json').read_text())['version'],
        'pyproject.toml': re.search(r'^version = "([^"]+)"', (ROOT / 'pyproject.toml').read_text(), re.M)[1],
        'remote_ssh/__init__.py': re.search(r"__version__ = '([^']+)'", (ROOT / 'remote_ssh/__init__.py').read_text())[1],
    }


def changelog_notes(version: str) -> str | None:
    """Return the body of the dated changelog section for `version`, or None if there is none."""
    text = (ROOT / 'CHANGELOG.md').read_text()
    match = re.search(rf'^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\n(.*?)(?=^## |\Z)', text, re.M | re.S)
    return match[1].strip() if match else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', help='release tag such as v0.1.0; also writes the notes to --notes')
    parser.add_argument('--notes', type=Path, help='file to write the changelog notes to')
    args = parser.parse_args()

    found = versions()
    if len(set(found.values())) != 1:
        print('version mismatch: ' + ', '.join(f'{f}={v}' for f, v in found.items()), file=sys.stderr)
        return 1
    version = next(iter(found.values()))
    if args.tag is None:
        print(version)
        return 0
    if args.tag != f'v{version}':
        print(f'tag {args.tag} does not match version {version}', file=sys.stderr)
        return 1
    notes = changelog_notes(version)
    if not notes:
        print(f'CHANGELOG.md has no dated section "## [{version}] - YYYY-MM-DD"', file=sys.stderr)
        return 1
    if args.notes:
        args.notes.write_text(notes + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
