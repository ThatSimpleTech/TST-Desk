"""Permission reporting for both platforms.

The Windows half carries the weight here. Windows gates nothing, so the easy
report is ``all_granted: true`` and silence — which would leave the model
believing a click into an elevated window worked. These tests pin the two limits
into the payload.
"""

from __future__ import annotations

import sys

import pytest

from tst_cu_mcp import permissions
from tst_cu_mcp.backends.windows import WindowsBackend


class TestMacOSReport:
    def test_both_granted(self) -> None:
        report = permissions.build_report(screen_recording=True, accessibility=True)
        assert report["all_granted"] is True
        assert report[permissions.SCREEN_RECORDING]["granted"] is True
        assert report[permissions.ACCESSIBILITY]["granted"] is True

    def test_no_fix_section_when_nothing_is_missing(self) -> None:
        report = permissions.build_report(screen_recording=True, accessibility=True)
        assert "fix" not in report

    @pytest.mark.parametrize(
        ("screen", "access", "expected"),
        [
            (False, True, [permissions.SCREEN_RECORDING]),
            (True, False, [permissions.ACCESSIBILITY]),
            (False, False, [permissions.SCREEN_RECORDING, permissions.ACCESSIBILITY]),
        ],
    )
    def test_fix_steps_appear_only_for_what_is_missing(
        self, screen: bool, access: bool, expected: list[str]
    ) -> None:
        report = permissions.build_report(screen_recording=screen, accessibility=access)
        assert report["all_granted"] is False
        assert sorted(report["fix"]) == sorted(expected)

    def test_host_caveat_is_always_present(self) -> None:
        # The single most common macOS support question: the grant belongs to the
        # launching app, needs a restart, and — when Settings already shows ON —
        # belongs to an older build and needs a reset (TD-4823).
        report = permissions.build_report(screen_recording=True, accessibility=True)
        assert "quit and reopen" in report["host_caveat"]
        assert "Allow" in report["host_caveat"]
        assert "older build" in report["host_caveat"]
        assert "Reset grants" in report["host_caveat"]

    def test_platform_is_labelled(self) -> None:
        report = permissions.build_report(screen_recording=True, accessibility=True)
        assert report["platform"] == "macos"


class TestWindowsReport:
    def test_nothing_is_gated(self) -> None:
        report = permissions.build_windows_report(elevated=False)
        assert report["all_granted"] is True
        assert report["screen_capture"]["gate"] == "none"
        assert report["input_control"]["gate"] == "none"

    def test_platform_is_labelled(self) -> None:
        assert permissions.build_windows_report(elevated=False)["platform"] == "windows"

    def test_both_silent_failure_modes_are_reported(self) -> None:
        # This is the criterion that matters: granted-but-ineffective must be
        # visible in the payload, not just in the docs.
        limits = permissions.build_windows_report(elevated=False)["limits"]
        assert "uipi" in limits
        assert "secure_desktop" in limits

    def test_uipi_text_explains_the_symptom_and_the_fix(self) -> None:
        text = permissions.build_windows_report(elevated=False)["limits"]["uipi"]
        assert "integrity level" in text
        assert "elevated" in text

    def test_secure_desktop_text_says_there_is_no_workaround(self) -> None:
        text = permissions.build_windows_report(elevated=False)["limits"]["secure_desktop"]
        assert "no workaround" in text

    def test_uipi_applies_only_when_not_elevated(self) -> None:
        # Elevated, we can drive anything; unelevated, admin windows will ignore
        # us. Reporting which case we are in is the actionable part.
        assert permissions.build_windows_report(elevated=False)["limits_apply"]["uipi"] is True
        assert permissions.build_windows_report(elevated=True)["limits_apply"]["uipi"] is False

    def test_secure_desktop_always_applies(self) -> None:
        for elevated in (True, False):
            report = permissions.build_windows_report(elevated=elevated)
            assert report["limits_apply"]["secure_desktop"] is True

    def test_elevation_is_reported(self) -> None:
        assert permissions.build_windows_report(elevated=True)["elevated"] is True
        assert permissions.build_windows_report(elevated=False)["elevated"] is False

    def test_absence_of_a_gate_is_explained_rather_than_implied(self) -> None:
        assert "TCC" in permissions.build_windows_report(elevated=False)["no_gate"]

    def test_no_macos_fix_steps_leak_in(self) -> None:
        report = permissions.build_windows_report(elevated=False)
        assert "fix" not in report
        assert "host_caveat" not in report


class TestDispatch:
    def test_check_permissions_routes_to_the_active_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(WindowsBackend, "check_permissions", lambda _s, **_k: {"spy": True})
        assert permissions.check_permissions() == {"spy": True}

    def test_request_flag_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[bool] = []
        monkeypatch.setattr(sys, "platform", "win32")

        def fake_check(_self: object, *, request: bool = False) -> dict[str, object]:
            seen.append(request)
            return {}

        monkeypatch.setattr(WindowsBackend, "check_permissions", fake_check)
        permissions.check_permissions(request=True)
        assert seen == [True]


class TestLinuxReport:
    def test_x11_usable_is_granted(self) -> None:
        report = permissions.build_linux_report(session="x11", display=True, xtest=True)
        assert report["all_granted"] is True
        assert report["platform"] == "linux"
        assert report["session_type"] == "x11"
        assert report["limits"] == {}

    def test_wayland_is_not_granted(self) -> None:
        report = permissions.build_linux_report(session="wayland", display=False, xtest=False)
        assert report["all_granted"] is False
        assert "wayland" in report["limits"]
        assert "TD-2002" in report["limits"]["wayland"]

    def test_missing_xtest_is_named(self) -> None:
        report = permissions.build_linux_report(session="x11", display=True, xtest=False)
        assert report["all_granted"] is False
        assert "xtest" in report["limits"]

    def test_no_macos_copy_leaks_in(self) -> None:
        report = permissions.build_linux_report(session="x11", display=True, xtest=True)
        assert "host_caveat" not in report
        assert "TCC" in report["no_gate"]


class TestLinuxDispatch:
    def test_linux_backend_accepts_and_ignores_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tst_cu_mcp.backends.linux import LinuxBackend

        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        without = LinuxBackend().check_permissions(request=False)
        with_request = LinuxBackend().check_permissions(request=True)
        assert without == with_request
        assert with_request["session_type"] == "wayland"


class TestWindowsBackendRequest:
    def test_windows_backend_accepts_and_ignores_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # There is no prompt to raise, but the interface has to stay symmetrical
        # or the tool would need a platform branch at the call site.
        monkeypatch.setattr("tst_cu_mcp.backends.windows.is_elevated", lambda: False)
        without = WindowsBackend().check_permissions(request=False)
        with_request = WindowsBackend().check_permissions(request=True)
        assert without == with_request
