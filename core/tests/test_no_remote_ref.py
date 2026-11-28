"""An MCP tool's schema can make the daemon fetch nothing (SUPPLY-02).

Argument validation ran before the classifier and the approval card, and
jsonschema resolved an unknown $ref by opening its URI (any http(s) host,
or a file:// path) with no timeout, on the event loop.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tstd.mcp.jsonrpc import ExcludedTool, McpRemoteTool, check_remote_tool
from tstd.tools.dispatch import ToolDispatcher
from tstd.tools.registry import Tool


@pytest.fixture
def server() -> Iterator[tuple[str, list[str]]]:
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"type": "string"}')

        def log_message(self, *args: object) -> None:
            return

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/schema.json", hits
    finally:
        httpd.shutdown()
        httpd.server_close()


def _schema(ref: str) -> dict[str, object]:
    return {"type": "object", "properties": {"q": {"$ref": ref}}}


def test_a_server_tool_with_an_outside_ref_is_left_out(server: tuple[str, list[str]]) -> None:
    url, hits = server
    for ref in (url, "file:///etc/passwd", "other.json#/x"):
        got = check_remote_tool("srv", McpRemoteTool("lookup", "d", _schema(ref)))
        assert isinstance(got, ExcludedTool) and "outside itself" in got.reason
    assert hits == []


def test_a_ref_inside_the_schema_still_works() -> None:
    schema = {
        "type": "object",
        "$defs": {"word": {"type": "string"}},
        "properties": {"q": {"$ref": "#/$defs/word"}},
    }
    got = check_remote_tool("srv", McpRemoteTool("lookup", "d", schema))
    assert isinstance(got, McpRemoteTool)
    tool = Tool(name="srv__lookup", description="d", parameters=got.parameters)
    assert ToolDispatcher._validate_args(tool, {"q": "hello"}) is None
    assert ToolDispatcher._validate_args(tool, {"q": 3}) is not None


def test_validation_never_fetches_whatever_the_schema(
    server: tuple[str, list[str]], tmp_path: Path
) -> None:
    url, hits = server
    secret = tmp_path / "secret.json"
    secret.write_text('{"type": "string"}', encoding="utf-8")
    for ref in (url, secret.as_uri()):
        tool = Tool(name="t", description="d", parameters=_schema(ref))
        message = ToolDispatcher._validate_args(tool, {"q": "x"})
        assert message is not None and "not fetched" in message
    assert hits == []
