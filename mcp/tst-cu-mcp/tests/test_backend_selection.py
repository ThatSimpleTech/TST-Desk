"""Backend selection, proven for both platforms from whichever host runs this.

The point of these tests is that they do not need the platform they describe.
Both backend modules are importable everywhere and construct without touching
the OS, so a Windows machine can prove the macOS branch and vice versa. Without
that property, half of this logic would only ever be exercised by a CI leg, which
is exactly how a platform port rots.
"""

from __future__ import annotations

import sys

import pytest

from tst_cu_mcp.backends import (
    SUPPORTED_PLATFORMS,
    Backend,
    UnsupportedPlatformError,
    backend_name,
    get_backend,
)
from tst_cu_mcp.backends.darwin import DarwinBackend
from tst_cu_mcp.backends.windows import WindowsBackend


class TestSelection:
    def test_darwin_selected_for_darwin(self) -> None:
        assert isinstance(get_backend("darwin"), DarwinBackend)

    def test_windows_selected_for_win32(self) -> None:
        assert isinstance(get_backend("win32"), WindowsBackend)

    def test_running_platform_resolves_without_an_argument(self) -> None:
        # Whichever host this is, the default must agree with the explicit form.
        assert type(get_backend()) is type(get_backend(sys.platform))

    def test_sys_platform_is_honoured_when_patched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The runtime path reads sys.platform rather than taking an argument, so
        # patching it is what proves the real selection, not the override.
        monkeypatch.setattr(sys, "platform", "darwin")
        assert isinstance(get_backend(), DarwinBackend)
        monkeypatch.setattr(sys, "platform", "win32")
        assert isinstance(get_backend(), WindowsBackend)

    def test_unsupported_platform_names_what_is_supported(self) -> None:
        with pytest.raises(UnsupportedPlatformError) as excinfo:
            get_backend("linux")
        message = str(excinfo.value)
        assert "linux" in message
        for platform in SUPPORTED_PLATFORMS:
            assert platform in message

    def test_selection_is_not_cached_across_platforms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A cache keyed on nothing would pin the first answer for the process and
        # make every later test in this file agree with it by accident.
        monkeypatch.setattr(sys, "platform", "darwin")
        first = get_backend()
        monkeypatch.setattr(sys, "platform", "win32")
        second = get_backend()
        assert not isinstance(second, type(first))


class TestBackendName:
    def test_names_are_distinct_and_stable(self) -> None:
        assert DarwinBackend().name == "darwin"
        assert WindowsBackend().name == "windows"

    def test_name_for_unsupported_platform_is_none_not_an_error(self) -> None:
        # health must answer on any host, including one we do not support.
        assert backend_name("linux") is None

    def test_name_for_supported_platforms(self) -> None:
        assert backend_name("darwin") == "darwin"
        assert backend_name("win32") == "windows"


class TestProtocolConformance:
    @pytest.mark.parametrize("backend", [DarwinBackend(), WindowsBackend()])
    def test_backend_satisfies_the_protocol(self, backend: object) -> None:
        assert isinstance(backend, Backend)

    @pytest.mark.parametrize("backend", [DarwinBackend(), WindowsBackend()])
    def test_every_contract_method_is_present_and_callable(self, backend: object) -> None:
        for method in (
            "list_displays",
            "capture_png",
            "move_mouse",
            "click",
            "type_text",
            "parse_key_combo",
            "press_keys",
            "scroll",
            "cursor_position",
            "foreground_window",
            "check_permissions",
        ):
            assert callable(getattr(backend, method)), method

    def test_constructing_a_backend_touches_no_os(self) -> None:
        # Constructing must stay free of OS calls: it is what lets the tests
        # above run on the wrong platform. If either constructor ever reaches
        # for a display list or a framework, this is where it fails.
        DarwinBackend()
        WindowsBackend()
