"""Sanitize MCP tool schemas for strict endpoints (TD-5104).

Covers the acceptance criteria: root ``anyOf`` / ``oneOf`` / ``allOf`` are
flattened where possible; a schema that still fails validation, or a name
a strict endpoint rejects, is excluded with its reason in the doctor row;
and one bad tool never fails a request.  The last is proved end to end: a
scripted MCP server lists good and bad tools, the supervisor loads them,
and a chat request carrying the registry's tools goes to a mock endpoint
that answers 400 to any tool a strict OpenAI-compatible server rejects.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import SchemaError
from jsonschema.validators import validator_for

from tstd.config import McpConfig, McpServerConfig
from tstd.mcp.jsonrpc import (
    ExcludedTool,
    McpRemoteTool,
    check_remote_tool,
    flatten_root_combinators,
    parse_tools_list,
    sanitize_remote_tools,
)
from tstd.mcp.loader import McpSupervisor
from tstd.provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ProviderClient,
)
from tstd.tools import ToolDispatcher, create_registry


def _tool(name: str, schema: dict[str, Any]) -> McpRemoteTool:
    return McpRemoteTool(name=name, description="", parameters=schema)


# ── flattening ───────────────────────────────────────────────────────────


class TestFlatten:
    def test_plain_schema_untouched(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        assert flatten_root_combinators(schema) is schema

    def test_any_of_merges_properties_and_keeps_common_required(self) -> None:
        schema = {
            "anyOf": [
                {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "id": {"type": "string"}},
                    "required": ["name", "id"],
                },
            ]
        }
        out = flatten_root_combinators(schema)
        assert "anyOf" not in out
        assert out["type"] == "object"
        assert set(out["properties"]) == {"id", "name"}
        assert out["required"] == ["id"]

    def test_one_of_with_nothing_in_common_requires_nothing(self) -> None:
        schema = {
            "oneOf": [
                {"properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"properties": {"b": {"type": "string"}}, "required": ["b"]},
            ]
        }
        out = flatten_root_combinators(schema)
        assert set(out["properties"]) == {"a", "b"}
        assert "required" not in out

    def test_all_of_requires_the_union(self) -> None:
        schema = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "allOf": [
                {"properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"properties": {"b": {"type": "string"}}, "required": ["b"]},
            ],
        }
        out = flatten_root_combinators(schema)
        assert set(out["properties"]) == {"x", "a", "b"}
        assert out["required"] == ["a", "b"]

    @pytest.mark.parametrize(
        "branches",
        [
            [{"type": "string"}],
            [{"type": "object", "anyOf": [{"properties": {}}]}],
            ["not a schema"],
            [],
        ],
    )
    def test_unflattenable_root_excludes_the_tool(self, branches: list[Any]) -> None:
        checked = check_remote_tool("srv", _tool("t", {"anyOf": branches}))
        assert isinstance(checked, ExcludedTool)
        assert checked.reason.startswith("input schema: root anyOf")


# ── names and validation ─────────────────────────────────────────────────


class TestCheckRemoteTool:
    def test_good_tool_kept_and_normalized(self) -> None:
        checked = check_remote_tool("srv", _tool("echo", {"properties": {"t": {"type": "string"}}}))
        assert isinstance(checked, McpRemoteTool)
        assert checked.parameters["type"] == "object"

    @pytest.mark.parametrize("name", ["files.read", "say hi", "naïve", "a/b"])
    def test_name_with_bad_characters_excluded(self, name: str) -> None:
        checked = check_remote_tool("srv", _tool(name, {"type": "object"}))
        assert isinstance(checked, ExcludedTool)
        assert "letters, digits, _ and -" in checked.reason

    def test_name_too_long_once_prefixed(self) -> None:
        name = "x" * 60  # 60 + len("srv__") = 65
        checked = check_remote_tool("srv", _tool(name, {"type": "object"}))
        assert isinstance(checked, ExcludedTool)
        assert "65 characters" in checked.reason

    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "properties": {"a": {"type": "strng"}}},
            {"type": "object", "required": "a"},
            {"type": "object", "properties": {"a": 5}},
        ],
    )
    def test_invalid_schema_excluded(self, schema: dict[str, Any]) -> None:
        checked = check_remote_tool("srv", _tool("t", schema))
        assert isinstance(checked, ExcludedTool)
        assert checked.reason.startswith("input schema is invalid")

    def test_split_keeps_order_and_lists_reasons(self) -> None:
        kept, excluded = sanitize_remote_tools(
            "srv",
            [
                _tool("a", {"type": "object"}),
                _tool("b.c", {"type": "object"}),
                _tool("d", {"type": "object"}),
            ],
        )
        assert [t.name for t in kept] == ["a", "d"]
        assert [e.name for e in excluded] == ["b.c"]

    def test_parse_keeps_root_combinators_for_the_check(self) -> None:
        listed = parse_tools_list(
            {"tools": [{"name": "t", "inputSchema": {"anyOf": [{"properties": {}}]}}]}
        )
        assert "anyOf" in listed[0].parameters


# ── end to end: one bad tool never fails a request ───────────────────────


_SCRIPTED_SERVER = r"""
import json
import sys

TOOLS = [
    {"name": "good", "description": "fine", "inputSchema": {
        "type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
    {"name": "union", "description": "flattened", "inputSchema": {"anyOf": [
        {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}]}},
    {"name": "dotted.name", "description": "bad name", "inputSchema": {"type": "object"}},
    {"name": "broken", "description": "bad schema", "inputSchema": {
        "type": "object", "properties": {"a": {"type": "strng"}}}},
]

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "scripted", "version": "1"}}
    elif msg["method"] == "tools/list":
        result = {"tools": TOOLS}
    else:
        result = {"content": [{"type": "text", "text": "ok"}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
"""

_STRICT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _strict_problem(tool: dict[str, Any]) -> str | None:
    """What a strict OpenAI-compatible server would reject, or None."""
    fn = tool.get("function") or {}
    if not _STRICT_NAME.match(str(fn.get("name", ""))):
        return f"invalid function name {fn.get('name')!r}"
    params = fn.get("parameters")
    if not isinstance(params, dict) or params.get("type") != "object":
        return "parameters must be an object schema"
    if any(k in params for k in ("anyOf", "oneOf", "allOf")):
        return "combinator at the root of parameters"
    try:
        validator_for(params).check_schema(params)
    except SchemaError as e:
        return f"invalid schema: {e.message}"
    return None


def _strict_endpoint(seen: list[list[str]]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        tools = body.get("tools") or []
        seen.append([t["function"]["name"] for t in tools])
        for tool in tools:
            problem = _strict_problem(tool)
            if problem:
                return httpx.Response(400, json={"error": {"message": problem}})
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    return httpx.MockTransport(handle)


class TestOneBadToolNeverFailsARequest:
    async def test_turn_succeeds_with_bad_tools_excluded(self, tmp_path: Path) -> None:
        script = tmp_path / "server.py"
        script.write_text(_SCRIPTED_SERVER)
        config = McpConfig(
            servers={
                "srv": McpServerConfig(transport="stdio", command=[sys.executable, str(script)])
            }
        )
        supervisor = McpSupervisor(config, handshake_timeout=10.0, call_timeout=10.0)
        registry = create_registry()
        try:
            rows = await supervisor.ensure_loaded()
            supervisor.attach(registry, ToolDispatcher(registry))
        finally:
            await supervisor.aclose()

        # The doctor row names each excluded tool and why.
        (row,) = rows
        assert row.status == "ok"
        assert {e.name for e in row.excluded} == {"dotted.name", "broken"}
        assert "2 tool(s) listed; 2 excluded" in row.detail
        assert "dotted.name (name may only use" in row.detail
        assert "broken (input schema is invalid" in row.detail
        assert row.fix

        names = {t.name for t in registry.list_tools()}
        assert {"srv__good", "srv__union"} <= names
        assert "srv__dotted.name" not in names and "srv__broken" not in names
        union = registry.get("srv__union")
        assert union is not None and "anyOf" not in union.parameters

        seen: list[list[str]] = []
        client = ProviderClient(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            client=httpx.AsyncClient(transport=_strict_endpoint(seen)),
        )
        request = ChatCompletionRequest(
            model="strict-model",
            messages=[ChatMessage(role="user", content="hi")],
            tools=registry.to_provider_definitions(),
        )
        response = await client.chat_completion(request)
        assert isinstance(response, ChatCompletionResponse), response
        assert "srv__good" in seen[0] and "fs_grep" in seen[0]

    async def test_strict_endpoint_really_rejects_a_bad_tool(self) -> None:
        """Guard the guard: the mock must 400 on what we exclude."""
        bad = {"type": "function", "function": {"name": "srv__dotted.name", "parameters": {}}}
        assert _strict_problem(bad) is not None
        union = {
            "type": "function",
            "function": {"name": "srv__u", "parameters": {"anyOf": [{"type": "object"}]}},
        }
        assert _strict_problem(union) is not None
