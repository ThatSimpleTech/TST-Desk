"""End-to-end proof that this is a working MCP server on this platform.

Everything else tests functions. This launches the real entry point as a
subprocess, speaks JSON-RPC to it over stdio, and checks the answers — which is
the only thing that proves the port did not break the surface a client actually
touches.

All requests are written up front and stdin is then closed, so there is no
readline deadlock to tune: the server processes the batch in order and exits at
EOF.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest
from mcp_types.version import LATEST_HANDSHAKE_VERSION

TOOL_NAMES = {
    "health",
    "check_permissions",
    "get_screen_info",
    "screenshot",
    "move_mouse",
    "click",
    "type_text",
    "press_keys",
    "scroll",
    "get_foreground_window",
    "get_cursor_position",
    "wait",
    "wait_for_window",
}

TIMEOUT_SECONDS = 60


def rpc(request_id: int | None, method: str, params: dict[str, Any] | None = None) -> str:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return json.dumps(message)


@pytest.fixture(scope="module")
def session() -> dict[str, Any]:
    """Run one stdio session and return its parsed responses keyed by request id."""
    requests = "\n".join(
        [
            rpc(
                1,
                "initialize",
                {
                    "protocolVersion": LATEST_HANDSHAKE_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "tst-cu-mcp-tests", "version": "0"},
                },
            ),
            rpc(None, "notifications/initialized"),
            rpc(2, "tools/list"),
        ]
    )

    completed = subprocess.run(
        [sys.executable, "-m", "tst_cu_mcp"],
        input=requests + "\n",
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )

    responses: dict[int, Any] = {}
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic path
            pytest.fail(
                "stdout carried a non-JSON line, which corrupts the protocol "
                f"channel: {line!r} ({exc})"
            )
        if isinstance(message, dict) and "id" in message:
            responses[message["id"]] = message

    return {
        "responses": responses,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "returncode": completed.returncode,
    }


class TestHandshake:
    def test_server_responds_to_initialize(self, session: dict[str, Any]) -> None:
        assert 1 in session["responses"], session["stderr"][-2000:]

    def test_initialize_is_not_an_error(self, session: dict[str, Any]) -> None:
        assert "error" not in session["responses"][1], session["responses"][1]

    def test_server_identifies_itself(self, session: dict[str, Any]) -> None:
        result = session["responses"][1]["result"]
        assert result["serverInfo"]["name"] == "tst-cu-mcp"

    def test_server_reports_its_version(self, session: dict[str, Any]) -> None:
        from tst_cu_mcp import __version__

        assert session["responses"][1]["result"]["serverInfo"]["version"] == __version__

    def test_protocol_version_is_negotiated(self, session: dict[str, Any]) -> None:
        assert session["responses"][1]["result"]["protocolVersion"]

    def test_instructions_describe_this_platform(self, session: dict[str, Any]) -> None:
        # The regression this closes: a Windows host used to be handed macOS
        # instructions, telling the model to expect cmd-shortcuts and TCC prompts.
        instructions = session["responses"][1]["result"].get("instructions", "")
        if sys.platform == "win32":
            assert "Windows" in instructions
            assert "macOS" not in instructions
        elif sys.platform == "darwin":
            assert "macOS" in instructions


class TestToolListing:
    def test_every_tool_is_advertised(self, session: dict[str, Any]) -> None:
        listed = {tool["name"] for tool in session["responses"][2]["result"]["tools"]}
        assert listed >= TOOL_NAMES

    def test_no_unexpected_tools(self, session: dict[str, Any]) -> None:
        listed = {tool["name"] for tool in session["responses"][2]["result"]["tools"]}
        assert listed == TOOL_NAMES

    def test_every_tool_has_a_description(self, session: dict[str, Any]) -> None:
        for tool in session["responses"][2]["result"]["tools"]:
            assert tool.get("description"), tool["name"]

    def test_press_keys_advertises_this_platforms_modifiers(self, session: dict[str, Any]) -> None:
        tools = {t["name"]: t for t in session["responses"][2]["result"]["tools"]}
        description = tools["press_keys"]["description"]
        if sys.platform == "win32":
            assert "ctrl" in description
            assert "aliases for ctrl" in description
        elif sys.platform == "darwin":
            assert "cmd" in description


class TestProtocolHygiene:
    def test_stdout_is_protocol_only(self, session: dict[str, Any]) -> None:
        # Any stray print would corrupt the channel. The fixture already fails on
        # unparseable lines; this asserts every line was a JSON-RPC envelope.
        for line in session["stdout"].splitlines():
            if line.strip():
                assert json.loads(line).get("jsonrpc") == "2.0"

    def test_server_exits_cleanly_at_eof(self, session: dict[str, Any]) -> None:
        assert session["returncode"] == 0, session["stderr"][-2000:]

    def test_no_traceback_on_stderr(self, session: dict[str, Any]) -> None:
        assert "Traceback" not in session["stderr"], session["stderr"][-2000:]
