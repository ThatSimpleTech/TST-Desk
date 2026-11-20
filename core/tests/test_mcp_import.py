"""Importing MCP servers from pasted JSON (TD-5253).

The parser is pure and the text is hostile. These pin the shapes other
clients use, what is refused or left out, that a token lands in the
keychain and never in ``config.yaml``, a log or the preview, and who may
send the verbs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests.test_add_model import _stop
from tests.test_mcp_peer import _drain, _fresh, _pair
from tests.test_mcp_secrets import MemoryKeychain, memory_keychain  # noqa: F401
from tests.test_models_on_phone import _seeded
from tstd import mcp_import, mcp_import_apply
from tstd.config import load_config
from tstd.mcp_import import McpImportError, parse_import, slug_id, strip_json_comments

TOKEN = "ghp_IMPORT_TEST_TOKEN_0123456789"
DESKTOP = {
    "mcpServers": {
        "GitHub Server_1": {
            "command": "npx",
            "args": ["-y", "@example/server-github"],
            "env": {"GITHUB_TOKEN": TOKEN, "PORT": 8080, "DEBUG": "1"},
        }
    }
}


def _one(data: Any) -> mcp_import.ImportServer:
    [server] = parse_import(json.dumps(data))
    return server


# ── Reading the text ───────────────────────────────────────────────────


def test_the_claude_desktop_shape() -> None:
    server = _one(DESKTOP)
    assert (server.source_name, server.id) == ("GitHub Server_1", "github-server-1")
    assert (server.transport, server.refused) == ("stdio", "")
    assert server.command == ("npx", "-y", "@example/server-github")
    assert {v.name: v.disposition for v in server.values} == {
        "GITHUB_TOKEN": "keychain",
        "PORT": "literal",
        "DEBUG": "literal",
    }


def test_vs_code_servers_cursor_urls_and_a_bare_entry() -> None:
    vscode = _one({"servers": {"files": {"type": "stdio", "command": "uvx", "args": ["files"]}}})
    assert (vscode.id, vscode.command) == ("files", ("uvx", "files"))
    cursor = _one({"mcpServers": {"docs": {"url": "http://127.0.0.1:9000/mcp"}}})
    assert (cursor.transport, cursor.url, cursor.off_box) == (
        "http",
        "http://127.0.0.1:9000/mcp",
        False,
    )
    assert cursor.notes == ("No type was given; assumed Streamable HTTP.",)
    alias = _one(
        {"mcpServers": {"d": {"type": "streamable-http", "url": "https://mcp.example.test/x"}}}
    )
    assert alias.off_box is True and alias.refused == "" and alias.notes == ()
    bare = _one({"command": "node", "args": ["server.js"]})
    assert (bare.source_name, bare.id, bare.command) == ("server", "server", ("node", "server.js"))


def test_comments_and_trailing_commas_are_allowed() -> None:
    text = """
    {
      // from the README
      "mcpServers": {
        "web": { "url": "https://mcp.example.test/a//b", /* inline */ "type": "http", },
      },
    }
    """
    [server] = parse_import(text)
    assert server.url == "https://mcp.example.test/a//b"
    assert (
        strip_json_comments('{"a": "x // y", "b": "/* z */",}') == '{"a": "x // y", "b": "/* z */"}'
    )


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ({"type": "sse", "url": "https://x.example.test/sse"}, "SSE"),
        ({"type": "ws", "url": "wss://x.example.test"}, "WebSocket"),
        ({"url": "https://x.example.test", "headersHelper": "sh get.sh"}, "command to run"),
        ({"url": "https://x.example.test", "oauth": {"clientId": "a"}}, "OAuth"),
        ({"command": "npx", "auth": {"CLIENT_ID": "a"}}, "OAuth"),
        ({"url": "http://x.example.test/mcp"}, "https"),
        ({"command": "${userHome}/bin/server"}, "placeholder"),
        ({"command": "node", "args": ["${workspaceFolder}/s.js"]}, "placeholder"),
        ({"url": "https://${HOST}/mcp"}, "placeholder"),
        ({"command": ""}, "no command"),
        ({"command": "node", "args": "server.js"}, "args"),
        ({"command": "node", "args": [True]}, "args"),
        ({"type": "carrier-pigeon", "command": "x"}, "type"),
        ({"command": "x", "env": {"BAD NAME": "v"}}, "env name"),
        (
            {"url": "https://x.example.test", "headers": {"Mcp-Session-Id": "abcdefgh1"}},
            "set by TST Desk",
        ),
        ("npx server", "not a JSON object"),
    ],
)
def test_what_is_refused(entry: Any, why: str) -> None:
    server = _one({"mcpServers": {"s": entry}})
    assert server.refused and why in server.refused, server.refused


def test_what_is_left_out_is_said() -> None:
    server = _one(
        {
            "mcpServers": {
                "s": {
                    "command": "sh",
                    "args": ["-c", "x"],
                    "envFile": ".env",
                    "cwd": "/tmp",
                    "timeout": 5,
                    "sandboxEnabled": True,
                    "headers": {"X": "abcdefgh1"},
                }
            }
        }
    )
    assert server.refused == ""
    assert set(server.not_imported) == {"envFile", "cwd", "timeout", "sandboxEnabled", "headers"}
    assert any("sandbox" in note for note in server.notes)
    assert any("runs a shell" in note for note in server.notes)


def test_values_that_must_be_typed() -> None:
    server = _one(
        {
            "mcpServers": {
                "s": {
                    "command": "x",
                    "env": {
                        "API_KEY": "${MY_KEY}",
                        "OTHER": "${env:OTHER:-d}",
                        "INPUT": "${input:token}",
                        "SHORT_TOKEN": "abc",
                        "MODE": "fast",
                        "NOTHING": None,
                    },
                }
            }
        }
    )
    assert {v.name: v.disposition for v in server.values} == {
        "API_KEY": "needs_input",
        "OTHER": "needs_input",
        "INPUT": "needs_input",
        "SHORT_TOKEN": "needs_input",
        "MODE": "literal",
    }
    header = _one(
        {"mcpServers": {"h": {"url": "https://x.example.test", "headers": {"X-Mode": "a"}}}}
    )
    assert [v.disposition for v in header.values] == ["needs_input"]


def test_ids_are_slugs_and_never_collide() -> None:
    assert slug_id("My Server_1") == "my-server-1"
    assert slug_id("123") == "mcp-123"
    assert slug_id("***") == "mcp"
    assert len(slug_id("x" * 80)) <= 32
    servers = parse_import(
        json.dumps({"mcpServers": {"A b": {"command": "x"}, "a-b": {"command": "y"}}})
    )
    assert [s.id for s in servers] == ["a-b", "a-b-2"]


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[1, 2]", "{}", '{"mcpServers": {}}', '{"mcpServers": 3}', "{" * 100_000],
)
def test_text_that_is_not_a_server_list_is_refused_without_quoting_it(text: str) -> None:
    with pytest.raises(McpImportError) as caught:
        parse_import(text)
    assert TOKEN not in str(caught.value) and len(str(caught.value)) < 200


def test_a_parse_error_gives_a_position_and_never_the_text() -> None:
    with pytest.raises(McpImportError) as caught:
        parse_import('{"mcpServers": {"s": {"env": {"K": "' + TOKEN + '" oops}}}}')
    assert "line 1" in str(caught.value) and TOKEN not in str(caught.value)


def test_caps() -> None:
    many = {"mcpServers": {f"s{i}": {"command": "x"} for i in range(mcp_import.MAX_SERVERS + 1)}}
    with pytest.raises(McpImportError, match="more than"):
        parse_import(json.dumps(many))
    with pytest.raises(McpImportError, match="KiB"):
        parse_import(" " * (mcp_import.MAX_TEXT_BYTES + 1))


def test_a_value_is_never_in_a_repr_or_a_refusal() -> None:
    server = _one(DESKTOP)
    assert TOKEN not in repr(server)
    bad = _one({"mcpServers": {"s": {"command": "x", "env": {"K": [TOKEN]}}}})
    assert bad.refused and TOKEN not in bad.refused


def test_the_parser_opens_nothing() -> None:
    """Neither module imports anything that reaches a network, a process or a file."""
    import ast

    allowed = {
        "__future__",
        "json",
        "re",
        "dataclasses",
        "typing",
        "collections.abc",
        "pathlib",
        "mcp_values",
        "net",
        "",
        "keychain_mcp",
        "config",
        "config_write",
        "logging",
        "mcp_import",
    }
    for module in (mcp_import, mcp_import_apply):
        tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name in allowed, f"{module.__name__} imports {name}"
    assert "open(" not in Path(mcp_import.__file__ or "").read_text(encoding="utf-8")


# ── At the socket ──────────────────────────────────────────────────────


def _frame(data: Any = DESKTOP, **choice: Any) -> dict[str, Any]:
    [server] = parse_import(json.dumps(data))
    return {
        "type": "import_mcp_servers",
        "text": json.dumps(data),
        "servers": [{"source_name": server.source_name, "id": server.id, **choice}],
    }


async def test_the_preview_carries_no_value_and_saves_nothing(
    tmp_path: Path,
    memory_keychain: MemoryKeychain,  # noqa: F811
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    try:
        before = daemon.config_path.read_text(encoding="utf-8")
        frame = {"type": "preview_mcp_import", "text": json.dumps(DESKTOP)}
        preview = await _fresh(ws, frame, "mcp_import_preview")
        assert preview["type"] == "mcp_import_preview" and preview["error"] == ""
        [server] = preview["servers"]
        assert (server["id"], server["exists"], server["off_box"]) == (
            "github-server-1",
            False,
            False,
        )
        assert server["command"] == ["npx", "-y", "@example/server-github"]
        assert {v["name"]: v["disposition"] for v in server["values"]} == {
            "GITHUB_TOKEN": "keychain",
            "PORT": "literal",
            "DEBUG": "literal",
        }
        assert TOKEN not in json.dumps(preview)
        assert all("value" not in v for v in server["values"])
        assert daemon.config_path.read_text(encoding="utf-8") == before
        assert memory_keychain._secrets == {}
        broken = await _fresh(
            ws, {"type": "preview_mcp_import", "text": "{nope"}, "mcp_import_preview"
        )
        assert broken["servers"] == [] and "not JSON" in broken["error"]
    finally:
        await _stop(task, ws)


async def test_an_import_puts_the_token_in_the_keychain_and_only_a_reference_in_config(
    tmp_path: Path,
    memory_keychain: MemoryKeychain,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    caplog.set_level(logging.DEBUG)
    try:
        state = await _fresh(ws, _frame())
        assert state["type"] == "setup_state", state
        [row] = [r for r in state["mcp_servers"] if r["id"] == "github-server-1"]
        assert row["command"] == ["npx", "-y", "@example/server-github"]
        spec = load_config(daemon.config_path).mcp.servers["github-server-1"]
        assert spec.env == {"GITHUB_TOKEN": "keychain:github_token", "PORT": "8080", "DEBUG": "1"}
        assert spec.allow_remote is False and spec.enabled is True
        text = daemon.config_path.read_text(encoding="utf-8")
        assert TOKEN not in text
        assert TOKEN in memory_keychain._secrets.values()
        assert any(
            "tst-mcp.github-server-1.github_token" in key for key in memory_keychain._secrets
        )
        assert TOKEN not in json.dumps(state)
        assert TOKEN not in caplog.text
    finally:
        await _stop(task, ws)


async def test_what_an_import_refuses_writes_nothing(
    tmp_path: Path,
    memory_keychain: MemoryKeychain,  # noqa: F811
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    try:
        before = daemon.config_path.read_text(encoding="utf-8")
        remote = {
            "mcpServers": {
                "web": {
                    "url": "https://mcp.example.test/x",
                    "headers": {"Authorization": "Bearer " + TOKEN},
                }
            }
        }
        needs = {"mcpServers": {"s": {"command": "x", "env": {"API_KEY": "${KEY}"}}}}
        attempts = [
            (_frame(remote), "another machine"),
            (_frame(needs), "type a value for API_KEY"),
            (_frame(needs, inputs={"API_KEY": "short"}), "at least 8"),
            (_frame(id="Not A Slug"), "lowercase slug"),
            (
                _frame({"mcpServers": {"s": {"type": "sse", "url": "https://x.example.test"}}}),
                "SSE",
            ),
            ({**_frame(), "servers": [{"source_name": "nobody", "id": "x"}]}, "no server named"),
            ({**_frame(), "text": "{nope"}, "not JSON"),
        ]
        for frame, why in attempts:
            reply = await _fresh(ws, frame)
            assert reply["type"] == "error" and reply["code"] == "bad_request", (why, reply)
            assert why in reply["message"], reply["message"]
            assert TOKEN not in reply["message"]
        assert daemon.config_path.read_text(encoding="utf-8") == before
        assert memory_keychain._secrets == {}
        # With the tick and a typed value they go in.
        ok = await _fresh(ws, _frame(remote, allow_remote=True))
        assert ok["type"] == "setup_state", ok
        web = load_config(daemon.config_path).mcp.servers["web"]
        assert web.allow_remote is True and web.headers == {
            "Authorization": "keychain:authorization"
        }
        ok = await _fresh(ws, _frame(needs, inputs={"API_KEY": "typed-value-0001"}))
        assert ok["type"] == "setup_state", ok
        assert "typed-value-0001" in memory_keychain._secrets.values()
        assert "typed-value-0001" not in daemon.config_path.read_text(encoding="utf-8")
        # The same id again is refused rather than replacing what is there.
        again = await _fresh(ws, _frame(remote, allow_remote=True))
        assert again["code"] == "bad_request" and "already listed" in again["message"]
    finally:
        await _stop(task, ws)


async def test_a_failed_config_write_leaves_no_keychain_item(
    tmp_path: Path,
    memory_keychain: MemoryKeychain,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _daemon, task, ws = await _seeded(tmp_path)
    try:

        def boom(*_args: object, **_kwargs: object) -> None:
            raise mcp_import_apply.ConfigError("disk full")

        monkeypatch.setattr(mcp_import_apply, "save_mcp_server", boom)
        reply = await _fresh(ws, _frame())
        assert reply["type"] == "error" and "disk full" in reply["message"]
        assert memory_keychain._secrets == {}
    finally:
        await _stop(task, ws)


async def test_from_a_phone_it_is_the_mcp_permission_and_the_computer_is_told(
    tmp_path: Path,
    memory_keychain: MemoryKeychain,  # noqa: F811
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    try:
        preview = {"type": "preview_mcp_import", "text": json.dumps(DESKTOP)}
        for frame in (preview, _frame()):
            reply = await _fresh(phone, frame, "mcp_import_preview")
            assert reply["type"] == "error" and reply["code"] == "phone_permission_off"
        assert memory_keychain._secrets == {}
        await _fresh(
            ws, {"type": "set_phone_permission", "capability": "mcp_servers", "enabled": True}
        )
        await _drain(phone)
        assert (await _fresh(phone, _frame()))["type"] == "setup_state"
        notices = [e for e in await _drain(ws) if e["type"] == "phone_change"]
        assert [n["summary"] for n in notices] == [
            "added MCP server 'github-server-1': npx -y @example/server-github"
        ]
        assert TOKEN not in json.dumps(notices)
    finally:
        await phone.close()
        await _stop(task, ws)
