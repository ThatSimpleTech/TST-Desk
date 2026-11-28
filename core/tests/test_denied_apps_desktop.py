"""The denied apps list holds for the pointer, keyboard and screenshot tools (CU-02)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_desktop_tools import _dispatcher
from tstd.cu_policy import CuPolicy, save_cu_policy
from tstd.desktop.mock import MockDesktopDriver


@pytest.fixture
def denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cu.yaml"
    monkeypatch.setenv("TST_CU_MCP_CONFIG", str(path))
    save_cu_policy(CuPolicy(denied_apps=("Slack", "1Password")), path)
    return path


CALLS = [
    ("desktop_screenshot", {}),
    ("desktop_move", {"x": 1, "y": 1}),
    ("desktop_click", {"x": 1, "y": 1}),
    ("desktop_type", {"text": "hi"}),
    ("desktop_scroll", {"dy": 3}),
]


@pytest.mark.parametrize(("name", "args"), CALLS)
async def test_nothing_happens_while_a_denied_app_is_in_front(
    tmp_path: Path, denied: Path, name: str, args: dict[str, object]
) -> None:
    driver = MockDesktopDriver()
    driver.foreground_app, driver.foreground_title = "Slack", "general"
    dispatcher, _ = _dispatcher(tmp_path, driver)
    result = await dispatcher.dispatch("tc", name, args)
    assert result.status == "error" and result.error_code == "denied_app"
    assert driver.actuations == [] and driver.calls == []


async def test_a_title_match_counts_too(tmp_path: Path, denied: Path) -> None:
    driver = MockDesktopDriver()
    driver.foreground_app, driver.foreground_title = "Firefox", "1Password — vault"
    dispatcher, _ = _dispatcher(tmp_path, driver)
    result = await dispatcher.dispatch("tc", "desktop_click", {"x": 1, "y": 1})
    assert result.error_code == "denied_app"


async def test_another_app_in_front_runs(tmp_path: Path, denied: Path) -> None:
    driver = MockDesktopDriver()
    driver.foreground_app, driver.foreground_title = "Terminal", "zsh"
    dispatcher, _ = _dispatcher(tmp_path, driver)
    result = await dispatcher.dispatch("tc", "desktop_click", {"x": 1, "y": 1})
    assert result.status == "success"


async def test_an_unreadable_front_window_fails_closed(
    tmp_path: Path, denied: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = MockDesktopDriver()

    async def broken() -> dict[str, object]:
        raise RuntimeError("no display")

    monkeypatch.setattr(driver, "foreground_window", broken)
    dispatcher, _ = _dispatcher(tmp_path, driver)
    result = await dispatcher.dispatch("tc", "desktop_type", {"text": "hi"})
    assert result.error_code == "denied_app_unknown"
    assert driver.actuations == []
