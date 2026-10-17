"""Plug-in tools for Jarvis from MCP servers listed in config/jarvis-mcp.json.

The file uses the same "mcpServers" format as Claude Code and other MCP
clients, so server setup instructions can usually be copied in as-is:

    {"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}},
        "docs":   {"url": "https://example.com/mcp", "allowedTools": ["search"]}
    }}

Set "disabled": true on a server to keep it in the file but not load it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modes import CONFIG_DIR

logger = logging.getLogger("jarvis.plugins")

PLUGINS_FILE = CONFIG_DIR / "jarvis-mcp.json"


class PluginConfigError(ValueError):
    """config/jarvis-mcp.json is malformed."""


@dataclass(frozen=True)
class PluginSpec:
    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    allowed_tools: tuple[str, ...] | None = None


def _spec(name: str, entry: Any) -> PluginSpec:
    if not isinstance(entry, dict):
        raise PluginConfigError(f"MCP server '{name}' must be an object.")
    command = entry.get("command", "")
    url = entry.get("url", "")
    if bool(command) == bool(url):
        raise PluginConfigError(
            f"MCP server '{name}' needs exactly one of 'command' or 'url'."
        )
    allowed = entry.get("allowedTools")
    return PluginSpec(
        name=name,
        command=str(command),
        args=tuple(str(arg) for arg in entry.get("args", ())),
        env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
        url=str(url),
        allowed_tools=tuple(allowed) if allowed is not None else None,
    )


def load_plugin_specs(path: Path = PLUGINS_FILE) -> list[PluginSpec]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PluginConfigError(f"{path.name} is not valid JSON: {exc}") from exc
    # A missing key is an error, not "no plug-ins": it's usually a typo.
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise PluginConfigError(f"{path.name} needs an 'mcpServers' object.")
    return [
        _spec(name, entry)
        for name, entry in servers.items()
        if not (isinstance(entry, dict) and entry.get("disabled"))
    ]


def build_mcp_servers(specs: list[PluginSpec]) -> list[Any]:
    """Create LiveKit MCP server clients. They connect when Jarvis starts."""
    if not specs:
        return []
    from livekit.agents.llm import mcp

    servers: list[Any] = []
    for spec in specs:
        if spec.url:
            servers.append(
                mcp.MCPServerHTTP(
                    url=spec.url,
                    allowed_tools=list(spec.allowed_tools)
                    if spec.allowed_tools is not None
                    else None,
                )
            )
        else:
            servers.append(
                mcp.MCPServerStdio(
                    command=spec.command,
                    args=list(spec.args),
                    # Keep PATH etc. so commands like npx are found.
                    env={**os.environ, **spec.env} if spec.env else None,
                    # Servers launched through npx can take a while on first run.
                    client_session_timeout_seconds=30,
                )
            )
        logger.info("loaded MCP plug-in %s", spec.name)
    return servers
