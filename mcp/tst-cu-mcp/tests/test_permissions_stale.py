"""Stale TCC grants and the host passthrough (TD-4823).

macOS pins each grant to the host's code signature. A grant made for an older
build shows ON in System Settings and still fails; re-prompting cannot repair
it. These tests pin three things: every piece of model-facing copy names the
reset as the fix, the host's diagnosis reaches the model verbatim, and a
socket client with no host never prompts or writes prompt stamps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.client.client import Client

from tst_cu_mcp import permissions, server
from tst_cu_mcp.backends import darwin
from tst_cu_mcp.backends.darwin import DarwinBackend
from tst_cu_mcp.permissions import ACCESSIBILITY, NO_HOST_FIX, SCREEN_RECORDING, build_report

_HOST_REPORT: dict[str, Any] = {
    "platform": "macos",
    "screen_recording": {"granted": False, "required_for": "x"},
    "accessibility": {"granted": True, "required_for": "y"},
    "all_granted": False,
    "actuation_path": "host",
    "identity": {"signing": "adhoc", "cdhash": "abc123", "bundled": True},
    "stale_grant_suspected": {"screen_recording": True, "accessibility": False},
    "fix": {"screen_recording": "Reset grants in TST Desk → Settings → Computer use."},
    "reset_supported": True,
}


@pytest.fixture(autouse=True)
def checkout_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout ``tst-cu-mcp``: not the host identity, no stamps on disk."""
    stamp = tmp_path / "macos-tcc-prompted"
    monkeypatch.setattr(darwin, "PROMPT_STAMP_DIR", stamp)
    darwin._prompted_this_process.clear()
    monkeypatch.delenv("TST_CU_MCP_HOST", raising=False)
    monkeypatch.setattr(darwin, "_is_host_identity", lambda: False)
    monkeypatch.setattr(darwin, "_sock_path", lambda: None)
    return stamp


class TestCopy:
    @pytest.mark.parametrize("name", [SCREEN_RECORDING, ACCESSIBILITY])
    def test_fix_explains_the_stale_grant(self, name: str) -> None:
        report = build_report(screen_recording=False, accessibility=False)
        text = report["fix"][name]
        assert "older build" in text
        assert "Reset grants" in text
        assert "tccutil reset" in text
        assert "quit and reopen" in text
        assert "request=true" in text

    def test_host_caveat_says_reprompting_cannot_repair(self) -> None:
        caveat = permissions.HOST_CAVEAT
        assert "older build" in caveat
        assert "Reset grants" in caveat
        assert "Cmd+Q" in caveat
        assert "cannot repair" in caveat

    def test_instructions_tell_the_model_to_stop_on_stale(self) -> None:
        text = server.instructions("darwin")
        assert "stale_grant_suspected" in text
        assert "Reset grants" in text

    async def test_tool_description_names_the_stale_flag(self) -> None:
        async with Client(server.build_server(), mode="legacy") as client:
            tools = (await client.list_tools()).tools
        tool = next(t for t in tools if t.name == "check_permissions")
        assert "stale_grant_suspected" in (tool.description or "")

    def test_input_failure_names_the_reset(self) -> None:
        with pytest.raises(RuntimeError) as err:
            darwin._require_ok(b"err\n", "click")
        text = str(err.value)
        assert "Reset grants" in text
        assert "tccutil reset Accessibility com.thatsimpletech.tstdesk" in text
        assert "Do not retry" in text

    def test_ok_passes(self) -> None:
        darwin._require_ok(b"ok\n", "click")


class TestHostPassthrough:
    def test_host_report_is_returned_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[str] = []

        def transact(line: str, *, binary: bool = False, timeout: float | None = None) -> bytes:
            sent.append(line)
            return json.dumps(_HOST_REPORT).encode("utf-8") + b"\n"

        monkeypatch.setattr(darwin, "_cu_transact", transact)
        report = DarwinBackend().check_permissions(request=True)
        assert sent == ["permissions request"]
        assert report == _HOST_REPORT
        assert report["stale_grant_suspected"]["screen_recording"] is True
        assert report["identity"]["signing"] == "adhoc"

    def test_no_host_is_named_and_never_prompts(
        self, monkeypatch: pytest.MonkeyPatch, checkout_client: Path
    ) -> None:
        monkeypatch.setattr(darwin, "_cg_preflight", lambda: False)
        monkeypatch.setattr(darwin, "_window_titles_visible", lambda: False)
        monkeypatch.setattr(darwin, "_ax_process_trusted", lambda: False)
        monkeypatch.setattr(darwin, "_ax_api_usable", lambda: False)

        def fail(*_a: object) -> bool:
            raise AssertionError("a socket client must not prompt or stamp")

        monkeypatch.setattr(darwin, "_request_screen_recording", fail)
        monkeypatch.setattr(darwin, "_request_accessibility", fail)
        monkeypatch.setattr(darwin, "_mark_prompted", fail)
        report = DarwinBackend().check_permissions(request=True)
        assert report["actuation_path"] == "none"
        assert report["fix"]["host"] == NO_HOST_FIX
        assert report["all_granted"] is False
        assert not checkout_client.exists()
        assert darwin._prompted_this_process == set()
