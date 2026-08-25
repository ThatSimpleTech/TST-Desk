"""Computer-use episode open/close tags (TD-3407)."""

from __future__ import annotations

import pytest

from tstd.desktop.mock import MockDesktopDriver
from tstd.protocol import CuSession
from tstd.session import Session


@pytest.mark.asyncio
async def test_first_cu_tool_opens_once_and_overlay_is_told() -> None:
    session = Session("/tmp/ws")
    driver = MockDesktopDriver()
    session._overlay_session = driver.set_overlay_session

    await session.open_cu_session("desktop_click")
    await session.open_cu_session("desktop_type")

    tags = [e for e in session.event_log.all_events if isinstance(e, CuSession)]
    assert len(tags) == 1
    assert tags[0].active is True
    assert driver.calls == [("overlay_session", {"active": True})]


@pytest.mark.asyncio
async def test_non_cu_tool_does_not_open() -> None:
    session = Session("/tmp/ws")
    await session.open_cu_session("fs_read")
    assert session.cu_session_active is False
    assert session.event_log.all_events == []


@pytest.mark.asyncio
async def test_close_emits_false_and_is_idempotent() -> None:
    session = Session("/tmp/ws")
    driver = MockDesktopDriver()
    session._overlay_session = driver.set_overlay_session
    await session.open_cu_session("browser_click")
    await session.close_cu_session()
    await session.close_cu_session()

    tags = [e for e in session.event_log.all_events if isinstance(e, CuSession)]
    assert [t.active for t in tags] == [True, False]
    assert driver.calls == [
        ("overlay_session", {"active": True}),
        ("overlay_session", {"active": False}),
    ]


@pytest.mark.asyncio
async def test_cancel_closes_the_episode() -> None:
    session = Session("/tmp/ws")
    await session.set_state("running")
    await session.open_cu_session("desktop_move")
    await session.cancel()
    tags = [e for e in session.event_log.all_events if isinstance(e, CuSession)]
    assert tags[-1].active is False
    assert session.cu_session_active is False

