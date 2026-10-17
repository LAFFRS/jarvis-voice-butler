import json
from pathlib import Path

import pytest
from livekit.agents.llm import mcp

import plugins
from plugins import PluginConfigError, build_mcp_servers, load_plugin_specs


def write(tmp_path: Path, data) -> Path:
    path = tmp_path / "jarvis-mcp.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_missing_file_means_no_plugins(tmp_path: Path) -> None:
    assert load_plugin_specs(tmp_path / "nope.json") == []


def test_shipped_file_has_no_plugins() -> None:
    assert load_plugin_specs(plugins.PLUGINS_FILE) == []


def test_loads_command_and_url_servers_and_skips_disabled(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        {
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "args": ["-y", "server-github"],
                    "env": {"TOKEN": "abc"},
                },
                "docs": {"url": "https://example.com/mcp", "allowedTools": ["search"]},
                "old": {"command": "x", "disabled": True},
            }
        },
    )

    specs = load_plugin_specs(path)

    assert [spec.name for spec in specs] == ["github", "docs"]
    assert specs[0].args == ("-y", "server-github")
    assert specs[0].env == {"TOKEN": "abc"}
    assert specs[1].allowed_tools == ("search",)


@pytest.mark.parametrize(
    "data",
    [
        {"servers": {}},
        {"mcpServers": {"bad": "npx"}},
        {"mcpServers": {"bad": {}}},
        {"mcpServers": {"bad": {"command": "x", "url": "http://y"}}},
    ],
)
def test_malformed_files_are_reported(tmp_path: Path, data) -> None:
    with pytest.raises(PluginConfigError):
        load_plugin_specs(write(tmp_path, data))


def test_invalid_json_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "jarvis-mcp.json"
    path.write_text("{oops", encoding="utf-8")

    with pytest.raises(PluginConfigError, match="not valid JSON"):
        load_plugin_specs(path)


def test_builds_livekit_mcp_clients(tmp_path: Path) -> None:
    specs = load_plugin_specs(
        write(
            tmp_path,
            {
                "mcpServers": {
                    "local": {"command": "npx", "args": ["server"]},
                    "remote": {"url": "https://example.com/mcp"},
                }
            },
        )
    )

    servers = build_mcp_servers(specs)

    assert isinstance(servers[0], mcp.MCPServerStdio)
    assert isinstance(servers[1], mcp.MCPServerHTTP)
    assert build_mcp_servers([]) == []
