"""Computer-use indicator prefs and screenshot hide flag (TD-3402)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.browser.mock import MockBrowserDriver
from tstd.cu_indicators import (
    CuIndicatorPrefs,
    cu_indicators_path,
    hide_real_display_for_screenshot,
    load_cu_indicators,
    real_display_overlay_hidden,
    reset_cu_indicators,
    save_cu_indicators,
    set_current_prefs,
)
from tstd.daemon import Daemon
from tstd.desktop import MockDesktopDriver
from tstd.protocol import PROTOCOL_VERSION
from tstd.tools.browser import browser_screenshot
from tstd.tools.desktop import desktop_screenshot


@pytest.fixture(autouse=True)
def _reset_indicator_globals() -> Any:
    reset_cu_indicators()
    yield
    reset_cu_indicators()


class TestCuIndicatorPersist:
    def test_absent_is_defaults(self, tmp_path: Path) -> None:
        prefs = load_cu_indicators(tmp_path)
        assert prefs.glow is True
        assert prefs.agent_cursor is True
        assert prefs.show_on_real_display is False

    def test_round_trip(self, tmp_path: Path) -> None:
        save_cu_indicators(
            tmp_path,
            CuIndicatorPrefs(glow=False, agent_cursor=False, show_on_real_display=True),
        )
        prefs = load_cu_indicators(tmp_path)
        assert prefs.glow is False
        assert prefs.agent_cursor is False
        assert prefs.show_on_real_display is True

    def test_lands_in_user_data_not_workspace(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        workspace = tmp_path / "ws"
        (workspace / ".tst").mkdir(parents=True)
        workspace_config = workspace / ".tst" / "config.yaml"
        workspace_config.write_text("policy:\n  rules: []\n", encoding="utf-8")
        before = workspace_config.read_text(encoding="utf-8")

        save_cu_indicators(data, CuIndicatorPrefs())

        assert cu_indicators_path(data).exists()
        assert not (workspace / "cu-indicators.yaml").exists()
        assert workspace_config.read_text(encoding="utf-8") == before

    def test_unreadable_or_junk_is_defaults(self, tmp_path: Path) -> None:
        path = cu_indicators_path(tmp_path)
        path.write_text(":::: not yaml", encoding="utf-8")
        prefs = load_cu_indicators(tmp_path)
        assert prefs == CuIndicatorPrefs()
        path.write_text("- just a list\n", encoding="utf-8")
        assert load_cu_indicators(tmp_path) == CuIndicatorPrefs()
        path.write_text("glow: false\n", encoding="utf-8")
        assert load_cu_indicators(tmp_path).glow is False
        assert load_cu_indicators(tmp_path).agent_cursor is True
        path.write_text('glow: "false"\n', encoding="utf-8")
        assert load_cu_indicators(tmp_path).glow is False
        path.write_text("", encoding="utf-8")
        assert load_cu_indicators(tmp_path) == CuIndicatorPrefs()


class TestHideFlag:
    def test_off_toggle_never_hides(self) -> None:
        set_current_prefs(CuIndicatorPrefs(show_on_real_display=False))
        seen: list[bool] = []
        with hide_real_display_for_screenshot():
            seen.append(real_display_overlay_hidden())
        assert seen == [False]
        assert real_display_overlay_hidden() is False

    def test_on_toggle_hides_only_inside_the_block(self) -> None:
        set_current_prefs(CuIndicatorPrefs(show_on_real_display=True))
        assert real_display_overlay_hidden() is False
        with hide_real_display_for_screenshot():
            assert real_display_overlay_hidden() is True
        assert real_display_overlay_hidden() is False

    async def test_desktop_screenshot_sets_hide_flag(self) -> None:
        set_current_prefs(CuIndicatorPrefs(show_on_real_display=True))
        driver = MockDesktopDriver()
        seen: list[bool] = []
        original = driver.screenshot

        async def _watch(display: int | None = None) -> str:
            seen.append(real_display_overlay_hidden())
            return await original(display=display)

        driver.screenshot = _watch  # type: ignore[method-assign]
        raw = await desktop_screenshot(object(), driver)
        body = json.loads(raw)
        assert body["png_base64"]
        assert seen == [True]
        assert real_display_overlay_hidden() is False

    async def test_browser_screenshot_sets_hide_flag(self, tmp_path: Path) -> None:
        set_current_prefs(CuIndicatorPrefs(show_on_real_display=True))
        driver = MockBrowserDriver()
        seen: list[bool] = []
        original = driver.screenshot_png

        async def _watch() -> bytes:
            seen.append(real_display_overlay_hidden())
            return await original()

        driver.screenshot_png = _watch  # type: ignore[method-assign]

        class _Session:
            def __init__(self, persist: Path) -> None:
                self.persist_dir = persist
                self.id = "s1"
                self.event_log = _Log()

        class _Log:
            def __init__(self) -> None:
                self.events: list[Any] = []

            async def add(self, event: Any) -> Any:
                self.events.append(event)
                return event

        persist = tmp_path / "sess"
        persist.mkdir()
        await browser_screenshot(_Session(persist), driver)
        assert seen == [True]
        assert real_display_overlay_hidden() is False

    async def test_desktop_screenshot_does_not_hide_when_toggle_off(self) -> None:
        set_current_prefs(CuIndicatorPrefs(show_on_real_display=False))
        driver = MockDesktopDriver()
        seen: list[bool] = []
        original = driver.screenshot

        async def _watch(display: int | None = None) -> str:
            seen.append(real_display_overlay_hidden())
            return await original(display=display)

        driver.screenshot = _watch  # type: ignore[method-assign]
        await desktop_screenshot(object(), driver)
        assert seen == [False]


async def _connect_and_handshake(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _start_daemon(tmp: Path) -> tuple[Daemon, asyncio.Task[Any]]:
    daemon = Daemon(data_dir=tmp)
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop_daemon(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _ask(ws: Any, msg: dict[str, Any]) -> dict[str, Any]:
    await ws.send(json.dumps(msg))
    return dict(json.loads(await ws.recv()))


class TestCuIndicatorSettingsWire:
    async def test_setup_state_defaults(self, tmp_path: Path) -> None:
        daemon, task = await _start_daemon(tmp_path)
        try:
            assert daemon.cu_indicators.glow is True
            assert daemon.cu_indicators.agent_cursor is True
            assert daemon.cu_indicators.show_on_real_display is False
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            resp = await _ask(ws, {"type": "get_setup_state"})
            assert resp["type"] == "setup_state"
            assert resp["cu_glow"] is True
            assert resp["cu_agent_cursor"] is True
            assert resp["cu_show_on_real_display"] is False
            await ws.close()
        finally:
            await _stop_daemon(task)

    async def test_set_cu_indicators_writes_what_load_reads(self, tmp_path: Path) -> None:
        daemon, task = await _start_daemon(tmp_path)
        try:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            resp = await _ask(
                ws,
                {
                    "type": "set_cu_indicators",
                    "glow": False,
                    "agent_cursor": True,
                    "show_on_real_display": True,
                },
            )
            assert resp["type"] == "setup_state"
            assert resp["cu_glow"] is False
            assert resp["cu_agent_cursor"] is True
            assert resp["cu_show_on_real_display"] is True
            loaded = load_cu_indicators(tmp_path)
            assert loaded.glow is False
            assert loaded.show_on_real_display is True
            text = cu_indicators_path(tmp_path).read_text(encoding="utf-8")
            assert "show_on_real_display: true" in text
            await ws.close()
        finally:
            await _stop_daemon(task)

    async def test_choice_survives_restart(self, tmp_path: Path) -> None:
        daemon, task = await _start_daemon(tmp_path)
        try:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            await _ask(
                ws,
                {
                    "type": "set_cu_indicators",
                    "glow": True,
                    "agent_cursor": False,
                    "show_on_real_display": False,
                },
            )
            await ws.close()
        finally:
            await _stop_daemon(task)

        daemon2, task2 = await _start_daemon(tmp_path)
        try:
            assert daemon2.cu_indicators.agent_cursor is False
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon2.ws_server.port}", daemon2.ws_server.token
            )
            resp = await _ask(ws, {"type": "get_setup_state"})
            assert resp["cu_agent_cursor"] is False
            await ws.close()
        finally:
            await _stop_daemon(task2)
