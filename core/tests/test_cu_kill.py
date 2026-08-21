"""In-window computer-use kill-switch (TD-3404).

The switch itself is TD-3301. This file covers the wire: ``set_cu_kill``
calls ``Daemon.set_computer_use_killed`` and answers with connection-scoped
``cu_kill_state``. Screenshot still runs when actuation is stopped.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_desktop_tools import _dispatcher
from tstd.autonomy import DecisionClass
from tstd.daemon import Daemon
from tstd.desktop import MockDesktopDriver
from tstd.protocol import CuKillState, parse_daemon_event


async def test_set_cu_kill_acks_and_delegates(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path)
    mock = MockDesktopDriver()
    daemon.desktop_driver = mock

    raw = await daemon._handle_message(
        json.dumps({"type": "set_cu_kill", "killed": True}),
        None,
    )
    assert raw is not None
    event = parse_daemon_event(raw)
    assert isinstance(event, CuKillState)
    assert event.killed is True
    assert event.seq == 1
    assert "session_id" not in json.loads(raw)
    assert mock.killed is True

    raw = await daemon._handle_message(
        json.dumps({"type": "set_cu_kill", "killed": False}),
        None,
    )
    assert raw is not None
    event = parse_daemon_event(raw)
    assert isinstance(event, CuKillState)
    assert event.killed is False
    assert mock.killed is False


async def test_killed_blocks_click_not_screenshot(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path)
    mock = MockDesktopDriver()
    daemon.desktop_driver = mock
    await daemon._handle_message(
        json.dumps({"type": "set_cu_kill", "killed": True}),
        None,
    )

    dispatcher, _ = _dispatcher(tmp_path, mock)
    click = await dispatcher.dispatch("c1", "desktop_click", {"x": 1, "y": 2})
    shot = await dispatcher.dispatch("c2", "desktop_screenshot", {})
    assert click.status == "error"
    assert click.error_code == "cu_killed"
    assert mock.actuations == []
    assert shot.status == "success"
    assert shot.decision_class is DecisionClass.A
    assert [name for name, _ in mock.calls] == ["screenshot"]
