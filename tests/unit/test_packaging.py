"""Packaging tests: where the plugin declares its MCP server."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_repository_root_has_no_project_mcp_config() -> None:
    #--- a root .mcp.json would load as a project server, without CLAUDE_PLUGIN_ROOT, in sessions started here ---
    assert not (ROOT / '.mcp.json').exists()


def test_manifest_declares_the_ssh_server_under_the_plugin_root() -> None:
    server = json.loads((ROOT / '.claude-plugin' / 'plugin.json').read_text())['mcpServers']['ssh']
    assert server['command'] == 'python3'
    assert server['args'] == ['${CLAUDE_PLUGIN_ROOT}/scripts/server.py']
    assert set(server['env']) == {'REMOTE_SSH_MESSAGING', 'REMOTE_SSH_POLL_INTERVAL', 'REMOTE_SSH_SSHFS_OPTIONS'}
