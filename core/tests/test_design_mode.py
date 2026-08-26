"""Design mode hit-test and attachment (TD-3403 / TD-3406).

Browser DOM or desktop AX. The mock returns a scripted node; the crop
travels as a UTF-8 JSON sidecar under TD-1709 caps — not a new wire type.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tstd.attachments import AttachmentLimits, decode_attachments
from tstd.browser import BrowserError, MockBrowserDriver, scripted_hit_node
from tstd.daemon import Daemon
from tstd.desktop import DesktopError, MockDesktopDriver, scripted_ax_hit_node
from tstd.protocol import Attachment, DesignHit, ToolCall, parse_daemon_event


async def _open(daemon: Daemon, path: Path) -> str:
    raw = json.dumps({"type": "open_workspace", "path": str(path)})
    response = await daemon._handle_message(raw, None)
    assert response is not None
    session_id: str = json.loads(response)["session_id"]
    return session_id


async def _shutdown(daemon: Daemon, session_id: str) -> None:
    runner = daemon.session_registry.get_runner(session_id)
    if runner is not None:
        await runner.cancel()
    await daemon._shutdown()


class TestMockHitTest:
    async def test_scripted_node_does_not_actuate(self) -> None:
        driver = MockBrowserDriver()
        node = await driver.hit_test(12.0, 34.0)
        assert node == scripted_hit_node(12.0, 34.0)
        assert node["xpath"] == "//*[@data-mock-point='12,34']"
        assert node["role"] == "button"
        assert driver.actuations == []
        assert driver.calls[-1][0] == "hit_test"

    async def test_crash_is_a_typed_refusal(self) -> None:
        driver = MockBrowserDriver(crash=True)
        with pytest.raises(BrowserError) as ei:
            await driver.hit_test(1.0, 1.0)
        assert ei.value.code == "driver_crash"


class TestDaemonHitTest:
    async def test_design_hit_test_returns_the_mock_node(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        ws = tmp_path / "ws"
        ws.mkdir()
        sid = await _open(daemon, ws)
        raw = await daemon._handle_message(
            json.dumps({"type": "design_hit_test", "session_id": sid, "x": 12.0, "y": 34.0}),
            None,
        )
        assert raw is not None
        event = parse_daemon_event(raw)
        assert isinstance(event, DesignHit)
        assert event.session_id == sid
        assert event.seq == 1
        assert event.xpath == "//*[@data-mock-point='12,34']"
        assert event.role == "button"
        assert event.box is not None
        assert event.box.width == 80.0
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        logged = [e for e in sess.event_log.events_from(1) if isinstance(e, DesignHit)]
        assert logged == []
        await _shutdown(daemon, sid)

    async def test_unknown_session_is_typed(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = await daemon._handle_message(
            json.dumps({"type": "design_hit_test", "session_id": "missing", "x": 1, "y": 1}),
            None,
        )
        assert raw is not None
        err = json.loads(raw)
        assert err["code"] == "session_not_found"
        await daemon._shutdown()

    async def test_desktop_surface_returns_ax_role(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        ws = tmp_path / "ws"
        ws.mkdir()
        sid = await _open(daemon, ws)
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        await sess.event_log.add(
            ToolCall(
                session_id=sid,
                tool_call_id="c1",
                name="desktop_screenshot",
                arguments={},
                seq=1,
            )
        )
        raw = await daemon._handle_message(
            json.dumps({"type": "design_hit_test", "session_id": sid, "x": 12.0, "y": 34.0}),
            None,
        )
        assert raw is not None
        event = parse_daemon_event(raw)
        assert isinstance(event, DesignHit)
        assert event.role == "AXButton"
        assert event.xpath is None
        assert event.attributes["AXIdentifier"] == "mock-target"
        assert event.box is not None
        assert event.box.width == 80.0
        logged = [e for e in sess.event_log.events_from(1) if isinstance(e, DesignHit)]
        assert logged == []
        await _shutdown(daemon, sid)


class TestMockDesktopHitTest:
    async def test_scripted_ax_node_does_not_actuate(self) -> None:
        driver = MockDesktopDriver()
        driver.set_killed(True)
        node = await driver.hit_test(12.0, 34.0)
        assert node == scripted_ax_hit_node(12.0, 34.0)
        assert node["role"] == "AXButton"
        assert driver.actuations == []
        assert driver.calls[-1][0] == "hit_test"

    async def test_permission_deny_is_typed(self) -> None:
        driver = MockDesktopDriver(permission_denied=True)
        with pytest.raises(DesktopError) as ei:
            await driver.hit_test(1.0, 1.0)
        assert ei.value.code == DesktopError.PERMISSION_DENIED
        assert driver.actuations == []


class TestDesignPickAttachment:
    def test_json_sidecar_with_data_url_crop_is_text(self) -> None:
        body = (
            json.dumps(
                {
                    "kind": "design_pick",
                    "xpath": "//*[@id='ok']",
                    "role": "button",
                    "attributes": {"id": "ok"},
                    "box": {"x": 10, "y": 20, "width": 80, "height": 24},
                    "styles": {"display": "inline-block"},
                    "crop_data_url": "data:image/png;base64,aaa",
                }
            )
            + "\n"
        )
        payload = base64.b64encode(body.encode()).decode()
        decoded = decode_attachments(
            [Attachment(name="design-pick.json", content_b64=payload)],
            AttachmentLimits(),
        )
        assert decoded[0].name == "design-pick.json"
        assert '"kind": "design_pick"' in decoded[0].text
        assert "data:image/png;base64,aaa" in decoded[0].text
