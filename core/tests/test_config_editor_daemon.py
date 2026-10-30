"""TD-5203: Settings → Configuration over a real daemon socket.

An edit reaches the file and the daemon's live config (the reload the other
Settings writers use), a refused edit changes neither, no secret is on the
wire, and only the computer itself may read or write through the editor.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.machine_helpers import Fleet, ask, window
from tstd.config import McpConfig
from tstd.daemon import Daemon

TOKEN = "td5203-wire-Zr6Kq1Vx8Np3"


@pytest.fixture
async def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Daemon]:
    fleet = Fleet(monkeypatch)
    try:
        yield await fleet.start(tmp_path / "a")
    finally:
        await fleet.close()


async def _editor(ws: Any) -> dict[str, Any]:
    return await ask(ws, {"type": "get_config_editor"}, "config_editor")


async def test_get_then_set_reaches_the_file_and_the_live_config(daemon: Daemon) -> None:
    async with window(daemon) as ws:
        view = await _editor(ws)
        assert view["root"] == "ModelConfig" and "TierConfig" in view["defs"]
        assert view["defaults"]['["workflow","max_parallel"]'] == 3
        assert view["values"]["workflow"]["max_parallel"] == 3
        edit = {"type": "set_config_value", "path": ["workflow", "max_parallel"], "value": 6}
        ack = await ask(ws, edit, "config_editor")
    assert ack["edit"] == {"action": "set", "path": ["workflow", "max_parallel"]}
    assert ack["values"]["workflow"]["max_parallel"] == 6
    assert ack["defs"] == {} and ack["defaults"] == {}  # fixed; the first reply had them
    assert ack["version"] != view["version"]
    assert daemon.config.workflow.max_parallel == 6
    assert "  max_parallel: 6\n" in daemon.config_path.read_text(encoding="utf-8")


async def test_a_refused_edit_changes_neither_the_file_nor_the_daemon(daemon: Daemon) -> None:
    before = daemon.config_path.read_bytes()
    async with window(daemon) as ws:
        edit = {"type": "set_config_value", "path": ["workflow", "max_parallel"], "value": 99}
        refused = await ask(ws, edit, "config_value_rejected")
    assert refused["code"] == "invalid" and refused["path"] == ["workflow", "max_parallel"]
    assert "16" in refused["message"]
    assert daemon.config_path.read_bytes() == before
    assert daemon.config.workflow.max_parallel == 3


async def test_reset_and_the_active_model(daemon: Daemon) -> None:
    async with window(daemon) as ws:
        await ask(
            ws,
            {"type": "set_config_value", "path": ["search", "max_results"], "value": 2},
            "config_editor",
        )
        ack = await ask(
            ws, {"type": "reset_config_value", "path": ["search", "max_results"]}, "config_editor"
        )
        assert ack["values"]["search"]["max_results"] == 8
        assert daemon.config.search.max_results == 8
        await ask(
            ws,
            {"type": "set_config_value", "path": ["active_preset"], "value": "budget"},
            "config_editor",
        )
    # The reload alone keeps the live model; the edit is the person picking one.
    assert daemon.config.active_preset == "budget"


async def test_an_mcp_edit_reloads_the_servers(
    daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    reloaded: list[McpConfig] = []

    async def record(config: McpConfig) -> None:
        reloaded.append(config)

    monkeypatch.setattr(daemon._mcp, "reload", record)
    async with window(daemon) as ws:
        edit = {"type": "set_config_value", "path": ["mcp", "tool_search", "mode"], "value": "off"}
        await ask(ws, edit, "config_editor")
    assert [c.tool_search.mode for c in reloaded] == ["off"]


async def test_no_secret_value_is_on_the_wire(daemon: Daemon) -> None:
    text = daemon.config_path.read_text(encoding="utf-8").replace(
        "mcp:\n  servers: {}\n",
        "mcp:\n  servers:\n    docs:\n      transport: stdio\n      command: [docs]\n"
        f"      env:\n        DOCS_TOKEN: {TOKEN}\n        REF: keychain:docs-ref\n",
        1,
    )
    daemon.config_path.write_text(text, encoding="utf-8")
    async with window(daemon) as ws:
        await ws.send(json.dumps({"type": "get_config_editor"}))
        raw = await ws.recv()
    assert isinstance(raw, str) and TOKEN not in raw and "docs-ref" not in raw
    env = json.loads(raw)["values"]["mcp"]["servers"]["docs"]["env"]
    assert env == {"DOCS_TOKEN": "config", "REF": "keychain"}


async def test_a_phone_may_not_read_or_write_the_config(daemon: Daemon) -> None:
    phone_key = daemon.remote_devices.add("Pixel")[1]
    before = daemon.config_path.read_bytes()
    frames = [
        {"type": "get_config_editor"},
        {"type": "set_config_value", "path": ["remote", "port"], "value": 40000},
        {"type": "reset_config_value", "path": ["remote", "bind"]},
    ]
    async with window(daemon, token=phone_key, path="/phone") as phone:
        for frame in frames:
            reply = await ask(phone, frame, "error")
            assert reply["code"] == "loopback_only", frame
            assert reply["in_reply_to"] == frame["type"]
    assert daemon.config_path.read_bytes() == before
