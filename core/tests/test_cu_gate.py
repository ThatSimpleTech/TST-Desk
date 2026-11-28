"""Off and Stop close computer use; a capture asks first (CU-01, CU-07).

"Enable computer use: Off" never reached the daemon's own desktop tools:
they stayed offered and ran. Stop left capture working on purpose. And a
whole-screen capture was Class A, so it ran with no card.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet, ask, window
from tests.test_security_suite import make_dispatcher
from tstd import cu_gate
from tstd.autonomy import Boundary, DecisionClassifier, DecisionRequest
from tstd.loop import _cu_offered
from tstd.provider import FunctionDefinition, ToolDefinition


def _defs(*names: str) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            type="function", function=FunctionDefinition(name=n, description="", parameters={})
        )
        for n in names
    ]


def test_a_closed_gate_offers_no_desktop_tool() -> None:
    defs = _defs("desktop_screenshot", "desktop_click", "fs_read")
    assert _cu_offered(defs) == defs
    cu_gate.bind(lambda: "computer use is switched off in Settings")
    offered = _cu_offered(defs)
    assert offered is not None
    assert [d.function.name for d in offered if d.function] == ["fs_read"]


async def test_a_closed_gate_refuses_a_desktop_call_capture_included(tmp_path: Path) -> None:
    cu_gate.bind(lambda: "computer use is stopped (Stop CU)")
    dispatcher = make_dispatcher(tmp_path)
    for name, args in (("desktop_screenshot", {}), ("desktop_click", {"x": 1, "y": 1})):
        result = await dispatcher.dispatch("tc", name, args)
        assert result.status == "error" and result.error_code == "computer_use_closed"
        assert "stopped" in result.output


def test_a_capture_asks_first() -> None:
    request = DecisionRequest(tool_name="desktop_screenshot", actuates=False)
    decision = DecisionClassifier(Boundary(workspace_root=Path("/w"))).classify(request)
    assert decision.decision_class is not None and decision.decision_class.value == "B"
    assert decision.rule is not None and decision.rule.id == "desktop-capture"


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


async def test_settings_off_and_stop_close_the_gate(
    tmp_path: Path, fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TST_CU_MCP_CONFIG", str(tmp_path / "cu.yaml"))
    monkeypatch.setattr("tstd.daemon.cu_stopfile.engage", lambda: tmp_path / "STOP")
    monkeypatch.setattr("tstd.daemon.cu_stopfile.clear", lambda: tmp_path / "STOP")
    daemon = await fleet.start(tmp_path / "d")
    assert cu_gate.closed_reason() is None
    async with window(daemon) as ws:
        off = {
            "type": "set_cu_policy",
            "enabled": False,
            "mode": "background",
            "unhide_on_finish": True,
            "denied_apps": [],
        }
        await ask(ws, off, "setup_state")
        assert cu_gate.closed_reason() == "computer use is switched off in Settings"
        await ask(ws, {**off, "enabled": True}, "setup_state")
        assert cu_gate.closed_reason() is None
        await ask(ws, {"type": "set_cu_kill", "killed": True}, "cu_kill_state")
        assert cu_gate.closed_reason() == "computer use is stopped (Stop CU)"
