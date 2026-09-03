"""The real-display glow must signal, never interfere.

Three rules, each pinned here against fakes — no helper process is ever
spawned (the package conftest installs a null overlay; these tests install
their own recording fakes):

* resolution: ``TST_CU_MCP_OVERLAY`` beats ``overlay.enabled`` in config,
  which beats the platform default (darwin on, elsewhere off).
* gating: activity lights the glow, a kill-switch refusal hides it, and
  any overlay failure degrades to a permanent no-op without breaking the
  actuation or capture it is attached to.
* ordering: a screenshot grab is bracketed by grab_begin/grab_end, and the
  backend's grab happens strictly between them.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tst_cu_mcp import capture, input_control, safety
from tst_cu_mcp.config import Config
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.overlay import (
    OVERLAY_ENV,
    NullOverlay,
    get_overlay,
    overlay_enabled,
    reset_overlay,
    set_overlay,
)
from tst_cu_mcp.overlay.darwin import DarwinOverlay

ONE_DISPLAY = [
    DisplayInfo(display_id=1, index=0, x=0, y=0, width=1920, height=1080, scale=1.0, is_main=True)
]


def _png_bytes() -> bytes:
    """A real (tiny) PNG for the fake backend to return."""
    from functools import lru_cache
    from io import BytesIO

    @lru_cache(maxsize=1)
    def build() -> bytes:
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (4, 4), "white").save(buf, format="PNG")
        return buf.getvalue()

    return build()


class RecordingTransport:
    """Fake helper connection: records commands, acks everything."""

    def __init__(self, *, ack: bool = True) -> None:
        self.commands: list[str] = []
        self.ack = ack
        self.closed = False

    def send(self, command: str, *, timeout: float = 1.0) -> bool:
        self.commands.append(command)
        return self.ack

    def close(self) -> None:
        self.closed = True


class RecordingOverlay:
    """Records which overlay entry points fired, in order."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def begin_session(self) -> None:
        self.calls.append("begin")

    def end_session(self) -> None:
        self.calls.append("end")

    def activity(self) -> None:
        self.calls.append("activity")

    def notify_blocked(self) -> None:
        self.calls.append("blocked")

    @contextmanager
    def grab_hidden(self) -> Iterator[None]:
        self.calls.append("grab_begin")
        try:
            yield
        finally:
            self.calls.append("grab_end")

    def shutdown(self) -> None:
        self.calls.append("shutdown")


@pytest.fixture(autouse=True)
def _isolate_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Same safety isolation as the other suites: temp stop-file, no config."""
    monkeypatch.setenv(safety.STOP_FILE_ENV, str(tmp_path / "STOP"))
    safety.set_config(None)
    yield
    safety.set_config(None)


class TestResolution:
    def test_env_truthy_beats_config_and_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OVERLAY_ENV, "1")
        safety.set_config(Config(overlay_enabled=False))
        assert overlay_enabled(platform="win32") is True

    def test_env_falsy_disables_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OVERLAY_ENV, "0")
        assert overlay_enabled(platform="darwin") is False

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_falsy_spellings(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(OVERLAY_ENV, raw)
        assert overlay_enabled(platform="darwin") is False

    def test_config_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OVERLAY_ENV, raising=False)
        safety.set_config(Config(overlay_enabled=False))
        assert overlay_enabled(platform="darwin") is False

    def test_darwin_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OVERLAY_ENV, raising=False)
        assert overlay_enabled(platform="darwin") is True

    def test_win32_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OVERLAY_ENV, raising=False)
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert overlay_enabled(platform="win32") is True

    def test_linux_x11_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OVERLAY_ENV, raising=False)
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert overlay_enabled(platform="linux") is True

    def test_wayland_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OVERLAY_ENV, raising=False)
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert overlay_enabled(platform="linux") is False


class TestGetOverlay:
    def test_env_off_resolves_null_without_spawning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OVERLAY_ENV, "0")
        reset_overlay()
        overlay = get_overlay()
        assert isinstance(overlay, NullOverlay)

    def test_spawn_failure_degrades_without_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OVERLAY_ENV, "1")
        reset_overlay()

        def explode() -> Any:
            raise RuntimeError("no window server here")

        overlay = DarwinOverlay(spawn=explode)
        overlay.activity()  # must not raise
        assert overlay.degraded is True
        overlay.activity()  # degraded stays a no-op, still no raise


class TestDarwinGating:
    def test_activity_shows_when_alive(self) -> None:
        transport = RecordingTransport()
        overlay = DarwinOverlay(spawn=lambda: transport)
        overlay.activity()
        assert transport.commands == ["show"]

    def test_session_stays_until_end(self) -> None:
        transport = RecordingTransport()
        overlay = DarwinOverlay(spawn=lambda: transport)
        overlay.begin_session()
        overlay.begin_session()
        overlay.end_session()
        assert transport.commands == ["show", "show", "hide"]

    def test_killswitch_forces_hide(self, tmp_path: Path) -> None:
        stop = tmp_path / "STOP"
        stop.write_text("", encoding="utf-8")
        transport = RecordingTransport()
        overlay = DarwinOverlay(spawn=lambda: transport)
        overlay.activity()
        assert transport.commands == ["hide"]

    def test_notify_blocked_hides(self) -> None:
        transport = RecordingTransport()
        overlay = DarwinOverlay(spawn=lambda: transport)
        overlay.notify_blocked()
        assert transport.commands == ["hide"]

    def test_grab_pair_brackets_and_survives_failed_begin(self) -> None:
        ok = RecordingTransport()
        overlay = DarwinOverlay(spawn=lambda: ok)
        with overlay.grab_hidden():
            ok.commands.append("<grab>")
        assert ok.commands == ["grab_begin", "<grab>", "grab_end"]

        dead = RecordingTransport(ack=False)
        overlay2 = DarwinOverlay(spawn=lambda: dead)
        with overlay2.grab_hidden():
            pass  # begin was never acked; end must not be sent either
        assert dead.commands == ["grab_begin"]
        assert overlay2.degraded is True

    def test_shutdown_closes_transport(self) -> None:
        transport = RecordingTransport()
        overlay = DarwinOverlay(spawn=lambda: transport)
        overlay.activity()
        overlay.shutdown()
        assert transport.closed is True


class TestToolHooks:
    """The five actions ping the overlay; capture brackets the grab."""

    @pytest.fixture
    def recorder(self) -> RecordingOverlay:
        overlay = RecordingOverlay()
        set_overlay(overlay)
        return overlay

    @pytest.fixture
    def fake_desktop(self, recorder: RecordingOverlay, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stub the backend and displays for both input_control and capture.

        The fake backend appends into the recorder's list too, so a test
        sees overlay events and backend acts interleaved in true order.
        """
        acts = recorder.calls

        def spy(name: str) -> Any:
            def _inner(*_args: Any, **_kwargs: Any) -> Any:
                acts.append(name)

            return _inner

        def fake_capture_png(_self: Any, _rect: Any) -> bytes:
            acts.append("grab")
            return _png_bytes()

        backend = type(
            "FakeBackend",
            (),
            {
                "move_mouse": spy("move"),
                "click": spy("click"),
                "type_text": spy("type"),
                "press_keys": spy("press"),
                "scroll": spy("scroll"),
                "capture_png": fake_capture_png,
                "parse_key_combo": lambda self, combo: combo,
            },
        )()
        monkeypatch.setattr(input_control, "get_backend", lambda: backend)
        monkeypatch.setattr(input_control, "list_displays", lambda: ONE_DISPLAY)
        monkeypatch.setattr(capture, "list_displays", lambda: ONE_DISPLAY)
        import tst_cu_mcp.backends as backends

        monkeypatch.setattr(backends, "get_backend", lambda: backend)

    @pytest.mark.parametrize(
        "action, act",
        [
            (lambda: input_control.move_mouse(10, 10), "move"),
            (lambda: input_control.click(10, 10), "click"),
            (lambda: input_control.type_text("hello"), "type"),
            (lambda: input_control.press_keys("ctrl+c"), "press"),
            (lambda: input_control.scroll(0, 3), "scroll"),
        ],
        ids=["move", "click", "type", "press", "scroll"],
    )
    def test_actuation_pokes_activity_first(
        self, recorder: RecordingOverlay, fake_desktop: None, action: Any, act: str
    ) -> None:
        action()
        # The glow lights on intent; the backend act follows it.
        assert recorder.calls == ["activity", act]

    def test_refusal_notifies_blocked(
        self, recorder: RecordingOverlay, fake_desktop: None, tmp_path: Path
    ) -> None:
        (tmp_path / "STOP").write_text("", encoding="utf-8")
        with pytest.raises(safety.KillSwitchEngaged):
            input_control.move_mouse(10, 10)
        assert recorder.calls == ["blocked"]

    def test_backend_refusal_puts_the_glow_out(
        self, recorder: RecordingOverlay, fake_desktop: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The OS said no (a missing Accessibility grant, say): the ring must
        not stay lit for an act that never happened."""

        class RefusingBackend:
            def parse_key_combo(self, combo: str) -> str:
                return combo

            def press_keys(self, _combo: str) -> None:
                raise RuntimeError("computer-use press_keys failed in the TST Desk host")

        monkeypatch.setattr(input_control, "get_backend", lambda: RefusingBackend())
        with pytest.raises(RuntimeError):
            input_control.press_keys("ctrl+c")
        assert recorder.calls == ["activity", "blocked"]

    def test_capture_brackets_backend_grab(
        self, recorder: RecordingOverlay, fake_desktop: None
    ) -> None:
        result = capture.capture(max_long_edge=64)
        assert result.png_bytes.startswith(b"\x89PNG")
        assert recorder.calls == ["grab_begin", "grab", "grab_end"]

    def test_capture_without_overlay_still_works(self, fake_desktop: None) -> None:
        set_overlay(NullOverlay())
        assert capture.capture(max_long_edge=64).png_bytes.startswith(b"\x89PNG")


def test_platform_resolution_uses_sys_platform_when_unspecified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """overlay_enabled() with no argument reads the running platform."""
    monkeypatch.delenv(OVERLAY_ENV, raising=False)
    assert overlay_enabled() is overlay_enabled(platform=sys.platform)
