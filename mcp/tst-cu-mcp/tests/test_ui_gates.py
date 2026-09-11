"""Kill-switch and denied-apps gate background actuation before the OS."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tst_cu_mcp import safety, ui
from tst_cu_mcp.apps import AppAccessError, AppInfo, AppNotFoundError
from tst_cu_mcp.config import Config
from tst_cu_mcp.safety import KillSwitchEngaged

SAFARI = AppInfo(name="Safari", pid=101, bundle_id="com.apple.Safari")
BANK = AppInfo(name="Bank", pid=202, bundle_id="com.example.bank")


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(safety.STOP_FILE_ENV, str(tmp_path / "STOP"))
    monkeypatch.setenv("TST_CU_MCP_CONFIG", str(tmp_path / "nope.yaml"))
    monkeypatch.delenv(safety.STOP_ENV, raising=False)
    safety.set_config(Config())
    monkeypatch.setattr(ui, "list_running", lambda: [SAFARI, BANK])
    yield
    safety.set_config(None)


def test_list_apps_omits_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    safety.set_config(Config(denied_apps=("Bank",)))
    payload = ui.list_apps()
    names = [app["name"] for app in payload["apps"]]
    assert names == ["Safari"]
    assert payload["mode"] == "background"


def test_snapshot_unknown_app_names_the_gap() -> None:
    with pytest.raises(AppNotFoundError, match="list_apps"):
        ui.ui_snapshot("Chess")


def test_snapshot_denied_app_is_refused() -> None:
    safety.set_config(Config(denied_apps=("Bank",)))
    with pytest.raises(AppAccessError, match="denied"):
        ui.ui_snapshot("Bank")


def test_action_is_blocked_by_kill_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reached: list[str] = []

    def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        reached.append("act")
        return {}

    monkeypatch.setattr(ui, "act_pid", boom)
    (tmp_path / "STOP").write_text("", encoding="utf-8")
    with pytest.raises(KillSwitchEngaged):
        ui.ui_action("Safari", "0.1", "press")
    assert reached == []


def test_action_reaches_backend_when_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, str, str]] = []

    def fake(pid: int, element_id: str, action: str, value: str | None = None) -> dict[str, Any]:
        seen.append((pid, element_id, action))
        return {"ok": True, "pid": pid, "id": element_id, "action": action}

    monkeypatch.setattr(ui, "act_pid", fake)
    result = ui.ui_action("Safari", "0.1", "press")
    assert seen == [(101, "0.1", "press")]
    assert result["app"]["pid"] == 101


def test_launch_denied_never_calls_open(monkeypatch: pytest.MonkeyPatch) -> None:
    reached: list[str] = []

    def fake_launch(name: str) -> dict[str, Any]:
        reached.append(name)
        return {}

    monkeypatch.setattr(ui, "launch_named", fake_launch)
    safety.set_config(Config(denied_apps=("1Password",)))
    with pytest.raises(AppAccessError):
        ui.launch_app("1Password")
    assert reached == []
