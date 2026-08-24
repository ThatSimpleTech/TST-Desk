"""Computer-use permission / integrity onboarding (TD-3302, TD-3303).

macOS: mock denied is a typed error with no actuation. Live MCP strings
that name Screen Recording or Accessibility map to the same code.

Windows: first-run names the missing grant dialog and the two silent
failure modes. UIPI / secure-desktop refuses are typed, not a hang.

Retry re-probes without ``request=True``. Settings reopen is
``check_cu_permissions``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_desktop_tools import _FAKE_MCP, _dispatcher
from tstd.daemon import Daemon
from tstd.desktop import DesktopError, McpDesktopDriver, MockDesktopDriver
from tstd.desktop.permissions import (
    ACCESSIBILITY_URL,
    FLAG_NAME,
    SCREEN_RECORDING_URL,
    WINDOWS_FLAG_NAME,
    WINDOWS_NO_GATE,
    build_cu_permissions,
    cu_permissions_from_report,
    is_permission_failure,
    is_secure_desktop_failure,
    is_uipi_failure,
    load_shown,
    mark_shown,
    parse_mcp_permissions_result,
    parse_probe,
    windows_report,
)
from tstd.desktop.stdio_mcp import map_mcp_error
from tstd.protocol import (
    CheckCuPermissions,
    CuPermissions,
    ToolCall,
    ToolResult,
    parse_client_message,
    parse_daemon_event,
)


class TestMockDeniedIsTyped:
    async def test_denied_click_does_not_actuate(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver(permission_denied=True)
        dispatcher, _ = _dispatcher(tmp_path, driver)
        result = await dispatcher.dispatch("c1", "desktop_click", {"x": 1, "y": 2})
        assert result.status == "error"
        assert result.error_code == DesktopError.PERMISSION_DENIED
        assert driver.actuations == []
        assert driver.calls == []

    async def test_denied_screenshot_does_not_record(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver(permission_denied=True)
        dispatcher, _ = _dispatcher(tmp_path, driver)
        result = await dispatcher.dispatch("c1", "desktop_screenshot", {})
        assert result.status == "error"
        assert result.error_code == DesktopError.PERMISSION_DENIED
        assert driver.calls == []

    async def test_check_permissions_reports_denied(self) -> None:
        driver = MockDesktopDriver(permission_denied=True)
        report = await driver.check_permissions()
        assert report["all_granted"] is False
        screen, access = parse_probe(report)
        assert screen is False
        assert access is False

    async def test_retry_after_grant_actuates(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver(permission_denied=True)
        dispatcher, _ = _dispatcher(tmp_path, driver)
        denied = await dispatcher.dispatch("c1", "desktop_click", {"x": 1, "y": 2})
        assert denied.error_code == DesktopError.PERMISSION_DENIED
        driver.permission_denied = False
        report = await driver.check_permissions()
        assert report["all_granted"] is True
        ok = await dispatcher.dispatch("c2", "desktop_click", {"x": 3, "y": 4})
        assert ok.status == "success"
        assert driver.actuations == ["click"]


class TestMcpPermissionMapping:
    def test_screen_recording_is_permission_denied(self) -> None:
        err = map_mcp_error(
            "screencapture produced no image; Screen Recording permission may be missing"
        )
        assert err.code == DesktopError.PERMISSION_DENIED

    def test_accessibility_is_permission_denied(self) -> None:
        err = map_mcp_error("Accessibility permission is required — call check_permissions.")
        assert err.code == DesktopError.PERMISSION_DENIED

    def test_unrelated_stays_cu_error(self) -> None:
        err = map_mcp_error("sidecar exploded")
        assert err.code == "cu_error"

    def test_sendinput_uipi_is_typed(self) -> None:
        err = map_mcp_error(
            "SendInput delivered 0 of 2 events (last error 5). "
            "A window running at a higher integrity level will silently discard "
            "synthetic input — see check_permissions for the UIPI limitation."
        )
        assert err.code == DesktopError.UIPI

    def test_secure_desktop_is_typed(self) -> None:
        err = map_mcp_error("The secure desktop cannot be captured or driven. Nothing was sent.")
        assert err.code == DesktopError.SECURE_DESKTOP

    def test_sidecar_secure_desktop_string_is_typed(self) -> None:
        # Exact wording raised by tst_cu_mcp.backends.windows._raise_if_secure_desktop.
        err = map_mcp_error(
            "UAC consent prompt on the secure desktop — capture and input cannot "
            "reach that session. There is no workaround."
        )
        assert err.code == DesktopError.SECURE_DESKTOP

    def test_is_permission_failure_is_narrow(self) -> None:
        assert is_permission_failure("TCC deny")
        assert not is_permission_failure("expected the foreground window")
        assert not is_uipi_failure("TCC deny")
        assert not is_secure_desktop_failure("TCC deny")
        assert is_uipi_failure("higher integrity level")
        assert is_secure_desktop_failure("UAC consent prompt on the secure desktop")

    async def test_live_sidecar_error_is_typed(self, tmp_path: Path) -> None:
        script = tmp_path / "deny_cu_mcp.py"
        script.write_text(
            _FAKE_MCP.replace(
                'if expect == "mismatch":',
                (
                    'if expect == "no-tcc":\n'
                    "            print(json.dumps({\n"
                    '                "jsonrpc": "2.0", "id": mid,\n'
                    '                "error": {"code": -32000, "message": '
                    '"Screen Recording permission is missing — call check_permissions."},\n'
                    "            }))\n"
                    '        elif expect == "mismatch":'
                ),
            ),
            encoding="utf-8",
        )
        import sys

        driver = McpDesktopDriver([sys.executable, str(script)], platform="darwin")
        try:
            with pytest.raises(DesktopError) as exc:
                await driver.click(1, 2, expect_window="no-tcc")
            assert exc.value.code == DesktopError.PERMISSION_DENIED
        finally:
            await driver.aclose()

    async def test_check_permissions_never_requests_tcc(self, tmp_path: Path) -> None:
        seen: list[dict[str, Any]] = []

        class _Client:
            async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
                seen.append({"name": name, "arguments": arguments or {}})
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "screen_recording": {"granted": True},
                                    "accessibility": {"granted": True},
                                    "all_granted": True,
                                }
                            ),
                        }
                    ]
                }

            async def aclose(self) -> None:
                return None

        driver = McpDesktopDriver(["/bin/false"], platform="darwin", client=_Client())  # type: ignore[arg-type]
        report = await driver.check_permissions()
        assert seen == [{"name": "check_permissions", "arguments": {"request": False}}]
        assert parse_probe(report) == (True, True)


class TestFirstRunFlagAndProtocol:
    def test_flag_is_in_user_data_dir(self, tmp_path: Path) -> None:
        assert load_shown(tmp_path) is False
        mark_shown(tmp_path)
        assert load_shown(tmp_path) is True
        assert (tmp_path / FLAG_NAME).is_file()
        # Workspace must not carry the flag.
        assert not (tmp_path / "workspace" / FLAG_NAME).exists()

    def test_event_carries_urls_and_denied(self) -> None:
        event = build_cu_permissions(screen_recording=False, accessibility=False, first_run=True)
        assert event.type == "cu_permissions"
        assert event.granted is False
        assert event.first_run is True
        assert event.screen_recording_url == SCREEN_RECORDING_URL
        assert event.accessibility_url == ACCESSIBILITY_URL
        assert event.platform == "macos"
        back = parse_daemon_event(event.model_dump_json())
        assert isinstance(back, CuPermissions)

    def test_check_message_round_trips(self) -> None:
        msg = parse_client_message(CheckCuPermissions().model_dump_json())
        assert isinstance(msg, CheckCuPermissions)

    def test_parse_mcp_permissions_from_text_block(self) -> None:
        raw = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"screen_recording": {"granted": False}, "all_granted": False}
                    ),
                }
            ]
        }
        screen, access = parse_probe(parse_mcp_permissions_result(raw))
        assert screen is False
        assert access is False


class TestDaemonCheckAndAnnounce:
    async def test_check_cu_permissions_replies(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path)
        daemon.desktop_driver = MockDesktopDriver(permission_denied=True)
        raw = await daemon._handle_message('{"type": "check_cu_permissions"}', None)
        assert raw is not None
        event = json.loads(raw)
        assert event["type"] == "cu_permissions"
        assert event["granted"] is False
        assert event["screen_recording"] is False
        assert event["accessibility"] is False
        assert event["screen_recording_url"] == SCREEN_RECORDING_URL
        assert event["first_run"] is False

    async def test_retry_path_flips_granted(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path)
        mock = MockDesktopDriver(permission_denied=True)
        daemon.desktop_driver = mock
        denied = json.loads(
            await daemon._handle_message('{"type": "check_cu_permissions"}', None) or ""
        )
        assert denied["granted"] is False
        mock.permission_denied = False
        granted = json.loads(
            await daemon._handle_message('{"type": "check_cu_permissions"}', None) or ""
        )
        assert granted["granted"] is True

    def test_first_desktop_tool_announces_once(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path)
        first = ToolCall(
            session_id="sess-1",
            tool_call_id="c1",
            name="desktop_click",
            arguments={"x": 1, "y": 2},
            seq=1,
        )
        assert daemon._cu_permissions_session(first) == "sess-1"
        mark_shown(tmp_path)
        assert daemon._cu_permissions_session(first) is None

    def test_permission_denied_result_reopens(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path)
        mark_shown(tmp_path)
        result = ToolResult(
            session_id="sess-1",
            tool_call_id="c1",
            status="error",
            output="denied",
            error_code=DesktopError.PERMISSION_DENIED,
            seq=2,
        )
        assert daemon._cu_permissions_session(result) == "sess-1"

    async def test_emit_does_not_wait_on_clients(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path)
        daemon.desktop_driver = MockDesktopDriver(permission_denied=True)
        sent: list[str] = []

        class _Conn:
            async def send(self, payload: str) -> None:
                sent.append(payload)

        daemon._attached_clients["sess-1"] = {_Conn()}
        await daemon._emit_cu_permissions("sess-1", first_run=True)
        assert load_shown(tmp_path) is True
        assert sent
        body = json.loads(sent[0])
        assert body["type"] == "cu_permissions"
        assert body["first_run"] is True
        assert body["granted"] is False
        assert body["platform"] == "macos"


class TestWindowsIntegrityOnboarding:
    async def test_uipi_click_does_not_actuate(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver(platform="win32", uipi_blocked=True)
        dispatcher, _ = _dispatcher(tmp_path, driver)
        result = await dispatcher.dispatch("c1", "desktop_click", {"x": 1, "y": 2})
        assert result.status == "error"
        assert result.error_code == DesktopError.UIPI
        assert driver.actuations == []
        assert driver.calls == []

    async def test_secure_desktop_screenshot_does_not_record(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver(platform="win32", secure_desktop_blocked=True)
        dispatcher, _ = _dispatcher(tmp_path, driver)
        result = await dispatcher.dispatch("c1", "desktop_screenshot", {})
        assert result.status == "error"
        assert result.error_code == DesktopError.SECURE_DESKTOP
        assert driver.calls == []

    async def test_win32_probe_is_granted_with_limits(self) -> None:
        driver = MockDesktopDriver(platform="win32")
        report = await driver.check_permissions()
        assert report["platform"] == "windows"
        assert report["all_granted"] is True
        assert report["no_gate"] == WINDOWS_NO_GATE
        assert "uipi" in report["limits"]
        assert "secure_desktop" in report["limits"]
        event = cu_permissions_from_report(report, first_run=True)
        assert event.platform == "windows"
        assert event.granted is True
        assert event.screen_recording_url == ""
        assert "TCC" in event.no_gate
        assert "integrity" in event.uipi.casefold()
        assert "elevated" in event.uipi.casefold()
        assert "no workaround" in event.secure_desktop.casefold()
        assert event.uipi_applies is True
        assert event.secure_desktop_applies is True
        back = parse_daemon_event(event.model_dump_json())
        assert isinstance(back, CuPermissions)
        assert back.platform == "windows"

    async def test_elevated_clears_live_uipi(self) -> None:
        driver = MockDesktopDriver(platform="win32", elevated=True)
        event = cu_permissions_from_report(await driver.check_permissions(), first_run=False)
        assert event.elevated is True
        assert event.uipi_applies is False
        assert event.secure_desktop_applies is True

    def test_macos_flag_does_not_suppress_windows_first_run(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path)
        daemon.desktop_driver = MockDesktopDriver(platform="win32")
        mark_shown(tmp_path)
        first = ToolCall(
            session_id="sess-1",
            tool_call_id="c1",
            name="desktop_click",
            arguments={"x": 1, "y": 2},
            seq=1,
        )
        assert daemon._cu_permissions_session(first) == "sess-1"
        mark_shown(tmp_path, "windows")
        assert daemon._cu_permissions_session(first) is None
        assert (tmp_path / WINDOWS_FLAG_NAME).is_file()
        assert (tmp_path / FLAG_NAME).is_file()

    @pytest.mark.parametrize("code", [DesktopError.UIPI, DesktopError.SECURE_DESKTOP])
    def test_integrity_refuse_reopens(self, tmp_path: Path, code: str) -> None:
        daemon = Daemon(data_dir=tmp_path)
        daemon.desktop_driver = MockDesktopDriver(platform="win32")
        mark_shown(tmp_path, "windows")
        result = ToolResult(
            session_id="sess-1",
            tool_call_id="c1",
            status="error",
            output="blocked",
            error_code=code,
            seq=2,
        )
        assert daemon._cu_permissions_session(result) == "sess-1"

    async def test_first_run_emit_is_windows_copy(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path)
        daemon.desktop_driver = MockDesktopDriver(platform="win32")
        sent: list[str] = []

        class _Conn:
            async def send(self, payload: str) -> None:
                sent.append(payload)

        daemon._attached_clients["sess-1"] = {_Conn()}
        await daemon._emit_cu_permissions("sess-1", first_run=True)
        assert load_shown(tmp_path, "windows") is True
        assert load_shown(tmp_path, "macos") is False
        body = json.loads(sent[0])
        assert body["type"] == "cu_permissions"
        assert body["platform"] == "windows"
        assert body["first_run"] is True
        assert body["granted"] is True
        assert "TCC" in body["no_gate"]
        assert "prompt" in body["no_gate"].casefold()
        assert "integrity" in body["uipi"].casefold()
        assert "no workaround" in body["secure_desktop"].casefold()
        assert body["screen_recording_url"] == ""

    async def test_check_cu_permissions_windows_copy(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path)
        daemon.desktop_driver = MockDesktopDriver(platform="win32")
        raw = await daemon._handle_message('{"type": "check_cu_permissions"}', None)
        assert raw is not None
        event = json.loads(raw)
        assert event["platform"] == "windows"
        assert event["first_run"] is False
        assert "TCC" in event["no_gate"]
        assert event["uipi_applies"] is True

    async def test_sidecar_windows_report_passes_through(self) -> None:
        class _Client:
            async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(windows_report(elevated=False)),
                        }
                    ]
                }

            async def aclose(self) -> None:
                return None

        driver = McpDesktopDriver(["/bin/false"], platform="win32", client=_Client())  # type: ignore[arg-type]
        report = await driver.check_permissions()
        event = cu_permissions_from_report(report, first_run=True)
        assert event.platform == "windows"
        assert "TCC" in event.no_gate
        assert event.uipi_applies is True
