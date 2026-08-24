"""Secure-desktop detection: the live path TD-3303 claimed and the mock already had.

The decision is a name comparison. The Win32 calls are mocked; a real UAC
prompt is not required to prove we refuse instead of returning a black frame.
"""

from __future__ import annotations

import pytest

from tst_cu_mcp.backends.windows import (
    WindowsBackend,
    _SECURE_DESKTOP_ERROR,
    input_desktop_is_ours,
)


class TestInputDesktopDecision:
    def test_unopened_input_is_secure(self) -> None:
        assert input_desktop_is_ours(False, "Default", "Default") is False

    def test_missing_names_are_secure(self) -> None:
        assert input_desktop_is_ours(True, None, "Default") is False
        assert input_desktop_is_ours(True, "Default", None) is False
        assert input_desktop_is_ours(True, "", "Default") is False

    def test_matching_names_are_ours(self) -> None:
        assert input_desktop_is_ours(True, "Default", "Default") is True

    def test_winlogon_is_not_ours(self) -> None:
        assert input_desktop_is_ours(True, "Winlogon", "Default") is False


class TestLiveRefuse:
    def test_error_text_names_the_markers_the_daemon_parses(self) -> None:
        lower = _SECURE_DESKTOP_ERROR.casefold()
        assert "secure desktop" in lower
        assert "uac consent" in lower

    def test_capture_raises_before_grabbing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tst_cu_mcp.backends.windows.is_secure_desktop", lambda: True)
        with pytest.raises(RuntimeError, match="secure desktop"):
            WindowsBackend().capture_png((0, 0, 10, 10))

    def test_move_raises_before_sendinput(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tst_cu_mcp.backends.windows.is_secure_desktop", lambda: True)
        with pytest.raises(RuntimeError, match="secure desktop"):
            WindowsBackend().move_mouse(0, 0)
