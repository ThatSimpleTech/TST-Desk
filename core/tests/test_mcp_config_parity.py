"""Settings does not lose an MCP server's keys (TD-5277).

``_mcp_server_lines`` writes a server's block from a list of keys kept by
hand, and the ``set_mcp_server`` handler copies the hand-set keys across
from a second list. Both are complete today. These tests are what makes a
new key on ``McpServerConfig`` fail here until someone has decided, for
each list, what happens to it: nothing was wrong, and nothing would have
said so the day something was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.catalog_helpers import catalog_config_yaml
from tests.test_add_model import _ask, _start, _stop
from tstd.config import McpServerConfig, load_config
from tstd.config_write import save_mcp_server
from tstd.mock import MockProvider, Script

# Every field with a value that is not its default, per transport. A field
# added to the model must be added here, which is the point.
EVERY_FIELD: dict[str, dict[str, Any]] = {
    "stdio": {
        "transport": "stdio",
        # Harmless if it ever ran: these tests start no server.
        "command": ["true", "--root", "/srv/data"],
        "url": "",
        "enabled": False,
        "timeout_seconds": 12.5,
        "startup_timeout_seconds": 45.0,
        "max_call_seconds": 900.0,
        "env": {"LOG_LEVEL": "debug", "API_TOKEN": "keychain:token"},
        "inherit_env": ["JAVA_HOME", "NODE_OPTIONS"],
        "headers": {},
        "read_only_tools": ["list_files", "read_file"],
        "allow_remote": False,
    },
    "http": {
        "transport": "http",
        "command": [],
        "url": "https://mcp.example.com/mcp",
        "enabled": False,
        "timeout_seconds": 12.5,
        "startup_timeout_seconds": 45.0,
        "max_call_seconds": 900.0,
        "env": {},
        "inherit_env": [],
        "headers": {"Authorization": "keychain:bearer", "X-Team": "tools"},
        "read_only_tools": ["search"],
        "allow_remote": True,
    },
}

# What the Settings page sends, what belongs to one server's address and is
# dropped when the transport or host changes, and what is carried across a
# Settings save untouched. Every field is in exactly one.
SENT_BY_SETTINGS = {"transport", "command", "url", "enabled"}
# ``inherit_env`` is bound tighter still: to the command (TD-5304).
BOUND_TO_THE_HOST = {"allow_remote", "env", "headers", "inherit_env"}
CARRIED = {"timeout_seconds", "startup_timeout_seconds", "max_call_seconds", "read_only_tools"}


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(catalog_config_yaml(), encoding="utf-8")
    return path


def test_the_table_names_every_field_of_the_model() -> None:
    for transport, values in EVERY_FIELD.items():
        assert set(values) == set(McpServerConfig.model_fields), transport


def test_every_field_is_sent_bound_or_carried_and_only_one_of_them() -> None:
    assert set(McpServerConfig.model_fields) == SENT_BY_SETTINGS | BOUND_TO_THE_HOST | CARRIED
    assert not SENT_BY_SETTINGS & BOUND_TO_THE_HOST
    assert not SENT_BY_SETTINGS & CARRIED
    assert not BOUND_TO_THE_HOST & CARRIED


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_the_writer_writes_every_field_and_it_reads_back_the_same(
    tmp_path: Path, transport: str
) -> None:
    path = _config(tmp_path)
    spec = McpServerConfig(**EVERY_FIELD[transport])
    save_mcp_server("walk", spec, path)

    block = yaml.safe_load(path.read_text(encoding="utf-8"))["mcp"]["servers"]["walk"]
    # The one of command and url the transport does not use is left out, and
    # a value at its default may be: everything set here is not.
    unused = "url" if transport == "stdio" else "command"
    empty = {name for name, value in EVERY_FIELD[transport].items() if value in ({}, [], "")}
    expected = set(McpServerConfig.model_fields) - {unused} - empty
    if not EVERY_FIELD[transport]["allow_remote"]:
        expected -= {"allow_remote"}
    assert expected <= set(block), sorted(expected - set(block))

    assert load_config(path).mcp.servers["walk"] == spec


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_a_settings_save_changes_only_what_settings_sent(
    tmp_path: Path, transport: str
) -> None:
    """The handler's own list: save what Settings sends and nothing else moves.

    The server is stored switched off and stays off, so nothing is started.
    """
    data = tmp_path / "data"
    data.mkdir()
    path = _config(data)
    spec = McpServerConfig(**EVERY_FIELD[transport])
    assert spec.enabled is False
    save_mcp_server("walk", spec, path)
    _daemon, task, ws = await _start(data, MockProvider(default=Script(kind="text")))
    try:
        wire = {name: getattr(spec, name) for name in SENT_BY_SETTINGS}
        reply = await _ask(ws, {"type": "set_mcp_server", "id": "walk", **wire}, "setup_state")
        assert reply["type"] == "setup_state", reply
        after = load_config(path).mcp.servers["walk"]
        changed = {
            name
            for name in McpServerConfig.model_fields
            if getattr(after, name) != getattr(spec, name)
        }
        assert changed == set()
    finally:
        await _stop(task, ws)


async def test_a_new_address_drops_what_was_bound_to_the_old_one_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    path = _config(data)
    spec = McpServerConfig(**EVERY_FIELD["stdio"])
    save_mcp_server("walk", spec, path)
    _daemon, task, ws = await _start(data, MockProvider(default=Script(kind="text")))
    try:
        reply = await _ask(
            ws,
            {
                "type": "set_mcp_server",
                "id": "walk",
                "transport": "http",
                "command": [],
                "url": "http://127.0.0.1:9000/mcp",
                "enabled": False,
            },
            "setup_state",
        )
        assert reply["type"] == "setup_state", reply
        after = load_config(path).mcp.servers["walk"]
        default = McpServerConfig(transport="http", url="http://127.0.0.1:9000/mcp")
        for name in BOUND_TO_THE_HOST:
            assert getattr(after, name) == getattr(default, name), name
        for name in CARRIED:
            assert getattr(after, name) == getattr(spec, name), name
    finally:
        await _stop(task, ws)


async def test_a_new_command_drops_the_inherited_names(tmp_path: Path) -> None:
    """TD-5304: the names were chosen for one program; another does not get them."""
    data = tmp_path / "data"
    data.mkdir()
    path = _config(data)
    spec = McpServerConfig(**EVERY_FIELD["stdio"])
    save_mcp_server("walk", spec, path)
    _daemon, task, ws = await _start(data, MockProvider(default=Script(kind="text")))
    try:
        reply = await _ask(
            ws,
            {
                "type": "set_mcp_server",
                "id": "walk",
                "transport": "stdio",
                "command": ["true", "--other"],
                "url": "",
                "enabled": False,
            },
            "setup_state",
        )
        assert reply["type"] == "setup_state", reply
        after = load_config(path).mcp.servers["walk"]
        assert after.inherit_env == []
        assert after.env == spec.env
    finally:
        await _stop(task, ws)
