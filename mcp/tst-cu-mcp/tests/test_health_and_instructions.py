"""What the server tells the model about the machine it is driving.

These strings are not documentation — the model reads them and acts on them. The
pre-port server announced "Local macOS computer-use server" and listed cmd/option
modifiers on every host, so a Windows model was told to reach for shortcuts that
do not exist and to expect permission prompts that never appear.
"""

from __future__ import annotations

import sys

import pytest

from tst_cu_mcp import server
from tst_cu_mcp.backends import SUPPORTED_PLATFORMS
from tst_cu_mcp.tools.health import SERVER_NAME, health_report


class TestHealthReport:
    def test_identifies_the_server(self) -> None:
        report = health_report()
        assert report["name"] == SERVER_NAME
        assert report["version"]

    def test_reports_the_running_platform(self) -> None:
        assert health_report()["platform"] == sys.platform

    @pytest.mark.parametrize("platform", SUPPORTED_PLATFORMS)
    def test_supported_platforms_are_supported(
        self, platform: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        if platform == "linux":
            monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
            monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
            monkeypatch.setenv("DISPLAY", ":0")
        assert health_report()["supported"] is True

    def test_unsupported_platform_reports_rather_than_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # health has to answer everywhere; it is what a client calls to find out
        # that the platform is wrong.
        monkeypatch.setattr(sys, "platform", "freebsd")
        report = health_report()
        assert report["supported"] is False
        assert report["backend"] is None

    def test_wayland_session_is_named_and_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        report = health_report()
        assert report["supported"] is False
        assert report["session_type"] == "wayland"
        assert report["backend"] is None

    @pytest.mark.parametrize(
        ("platform", "expected"),
        [("darwin", "darwin"), ("win32", "windows"), ("linux", "linux")],
    )
    def test_active_backend_is_named(
        self, platform: str, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        if platform == "linux":
            monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
            monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
            monkeypatch.setenv("DISPLAY", ":0")
        assert health_report()["backend"] == expected

    def test_supported_platforms_are_listed(self) -> None:
        assert health_report()["supported_platforms"] == list(SUPPORTED_PLATFORMS)

    def test_no_longer_hardcodes_darwin_as_the_supported_platform(self) -> None:
        # The specific regression: `platform` used to be the constant "darwin".
        monkeypatch_free = health_report()
        assert monkeypatch_free["platform"] == sys.platform


class TestInstructions:
    def test_macos_instructions_name_macos_and_its_permissions(self) -> None:
        text = server.instructions("darwin")
        assert "macOS" in text
        assert "Screen Recording" in text
        assert "cmd" in text

    def test_macos_instructions_tell_the_model_not_to_reprompt(self) -> None:
        text = server.instructions("darwin")
        assert "at most once" in text
        assert "Cmd+Q" in text

    def test_windows_instructions_name_windows_and_its_modifiers(self) -> None:
        text = server.instructions("win32")
        assert "Windows" in text
        assert "ctrl" in text
        assert "win/super" in text

    def test_windows_instructions_do_not_claim_macos(self) -> None:
        text = server.instructions("win32")
        assert "macOS" not in text
        assert "Screen Recording" not in text
        assert "Accessibility" not in text

    def test_macos_instructions_do_not_mention_windows_limits(self) -> None:
        text = server.instructions("darwin")
        assert "secure desktop" not in text
        assert "elevated" not in text

    def test_windows_instructions_disclose_both_silent_failures(self) -> None:
        text = server.instructions("win32")
        assert "elevated" in text
        assert "secure desktop" in text

    def test_windows_instructions_disclose_the_cmd_alias(self) -> None:
        # If the model is not told, it cannot know why cmd+c worked.
        assert "alias for ctrl" in server.instructions("win32")

    def test_linux_instructions_require_x11(self) -> None:
        text = server.instructions("linux")
        assert "X11" in text
        assert "Wayland" in text
        assert "ctrl" in text

    def test_unsupported_platform_is_stated_plainly(self) -> None:
        assert "not supported" in server.instructions("freebsd")

    def test_no_platform_argument_uses_the_running_host(self) -> None:
        assert server.instructions() == server.instructions(sys.platform)

    def test_the_shared_preamble_survives_on_every_platform(self) -> None:
        for platform in ("darwin", "win32", "linux"):
            text = server.instructions(platform)
            assert "no per-action approval gate" in text

    def test_instructions_prefer_background_tools(self) -> None:
        text = server.instructions("darwin")
        assert "ui_snapshot" in text
        assert "ui_action" in text
        assert "list_apps" in text


class TestPermissionHint:
    def test_macos_hint_names_the_permissions(self) -> None:
        hint = server._permission_hint("darwin")
        assert "Screen Recording" in hint
        assert "Accessibility" in hint

    def test_windows_hint_denies_a_permission_and_warns_about_elevation(self) -> None:
        hint = server._permission_hint("win32")
        assert "No permission needed" in hint
        assert "elevated" in hint

    def test_linux_hint_names_x11_and_wayland(self) -> None:
        hint = server._permission_hint("linux")
        assert "X11" in hint
        assert "Wayland" in hint

    def test_unknown_platform_defers_to_the_tool(self) -> None:
        assert "check_permissions" in server._permission_hint("freebsd")


class TestComboHelp:
    def test_macos_lists_mac_modifiers(self) -> None:
        text = server._combo_help("darwin")
        assert "cmd/command" in text
        assert "fn" in text

    def test_windows_leads_with_ctrl(self) -> None:
        text = server._combo_help("win32")
        assert "'ctrl+c'" in text

    def test_windows_explains_the_alias_and_the_refusal(self) -> None:
        text = server._combo_help("win32")
        assert "aliases for ctrl" in text
        assert "refused" in text

    def test_windows_does_not_advertise_fn_as_usable(self) -> None:
        text = server._combo_help("win32")
        assert "does not exist" in text

    def test_linux_leads_with_ctrl_and_refuses_fn(self) -> None:
        text = server._combo_help("linux")
        assert "'ctrl+c'" in text
        assert "refused" in text


class TestServerConstruction:
    def test_the_server_builds_on_this_host(self) -> None:
        # A smoke test with real value: it proves the MCP SDK version in use still
        # accepts every tool signature after the port rewired them.
        built = server.build_server()
        assert built is not None

    def test_building_twice_yields_independent_servers(self) -> None:
        assert server.build_server() is not server.build_server()
