"""When the ring shows and where (TD-5226), pinned against a recording painter.

* Mode: only full control lights it; background never does, and a mode
  flip while lit darkens or relights it on the next watcher tick.
* Display: the painter is aimed before it is lit, and re-aimed while lit.
* Episodes: one per client session; the ring holds while any is open.
* Non-bracketing clients: the ring fades after the linger, and a client
  that brackets never falls back to the linger.
* Stop and refusals darken it until the next action.
"""

from __future__ import annotations

import io
import queue
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tst_cu_mcp import safety
from tst_cu_mcp.config import Config, save_config
from tst_cu_mcp.overlay import OVERLAY_ENV, get_overlay, reset_overlay
from tst_cu_mcp.overlay.control import GatedOverlay, PolicyMode
from tst_cu_mcp.overlay.darwin_helper import _parse_display_id, _read_stdin, lit_keys


class Painter:
    name = "painter"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def begin_session(self) -> None:
        self.calls.append("begin")

    def end_session(self) -> None:
        self.calls.append("hide")

    def activity(self) -> None:
        self.calls.append("show")

    def notify_blocked(self) -> None:
        self.calls.append("blocked")

    def set_target(self, display_id: int | None) -> None:
        self.calls.append(f"target:{display_id}")

    @contextmanager
    def grab_hidden(self) -> Iterator[None]:
        self.calls.append("grab_begin")
        try:
            yield
        finally:
            self.calls.append("grab_end")

    def shutdown(self) -> None:
        self.calls.append("shutdown")


class World:
    """Mode, Stop and time the controller reads, all set by the test."""

    def __init__(self) -> None:
        self.mode = "full_control"
        self.stopped = False
        self.now = 1000.0


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def painter() -> Painter:
    return Painter()


@pytest.fixture
def ring(world: World, painter: Painter) -> GatedOverlay:
    return GatedOverlay(
        painter,
        mode=lambda: world.mode,
        stopped=lambda: world.stopped,
        clock=lambda: world.now,
        linger=30.0,
        watch=False,
    )


class TestMode:
    def test_background_never_lights(
        self, ring: GatedOverlay, painter: Painter, world: World
    ) -> None:
        world.mode = "background"
        ring.set_target(51)
        ring.begin_session("s1")
        ring.activity()
        ring.tick()
        assert painter.calls == []

    def test_full_control_lights_the_aimed_display(
        self, ring: GatedOverlay, painter: Painter
    ) -> None:
        ring.set_target(51)
        ring.begin_session("s1")
        assert painter.calls == ["target:51", "show"]

    def test_flip_to_background_darkens_and_back_relights(
        self, ring: GatedOverlay, painter: Painter, world: World
    ) -> None:
        ring.begin_session("s1")
        world.mode = "background"
        ring.tick()
        world.mode = "full_control"
        ring.tick()
        assert painter.calls == ["show", "hide", "show"]


class TestDisplay:
    def test_retarget_while_lit_moves_the_ring(self, ring: GatedOverlay, painter: Painter) -> None:
        ring.begin_session("s1")
        ring.set_target(51)
        ring.set_target(51)
        ring.set_target(53)
        assert painter.calls == ["show", "target:51", "target:53"]

    def test_target_while_dark_waits_for_the_ring(
        self, ring: GatedOverlay, painter: Painter
    ) -> None:
        ring.set_target(51)
        ring.set_target(53)
        assert painter.calls == []
        ring.activity()
        assert painter.calls == ["target:53", "show"]


class TestEpisodes:
    def test_ring_holds_while_any_session_is_open(
        self, ring: GatedOverlay, painter: Painter
    ) -> None:
        ring.begin_session("a")
        ring.begin_session("b")
        ring.end_session("a")
        assert painter.calls == ["show"]
        ring.end_session("b")
        assert painter.calls == ["show", "hide"]

    def test_a_bracketing_client_never_lingers(
        self, ring: GatedOverlay, painter: Painter, world: World
    ) -> None:
        ring.begin_session("a")
        ring.activity()
        ring.end_session("a")
        world.now += 1.0
        ring.tick()
        assert painter.calls == ["show", "hide"]


class TestUnbracketed:
    def test_activity_lights_then_fades_after_the_linger(
        self, ring: GatedOverlay, painter: Painter, world: World
    ) -> None:
        ring.activity()
        world.now += 29.0
        ring.tick()
        assert painter.calls == ["show"]
        world.now += 2.0
        ring.tick()
        assert painter.calls == ["show", "hide"]

    def test_new_activity_restarts_the_linger(
        self, ring: GatedOverlay, painter: Painter, world: World
    ) -> None:
        ring.activity()
        world.now += 20.0
        ring.activity()
        world.now += 20.0
        ring.tick()
        assert painter.calls == ["show"]


class TestStop:
    def test_stop_darkens_on_the_next_tick(
        self, ring: GatedOverlay, painter: Painter, world: World
    ) -> None:
        ring.activity()
        world.stopped = True
        ring.tick()
        assert painter.calls == ["show", "hide"]

    def test_activity_while_stopped_stays_dark_and_resume_needs_an_action(
        self, ring: GatedOverlay, painter: Painter, world: World
    ) -> None:
        ring.activity()
        world.stopped = True
        ring.activity()
        world.stopped = False
        ring.tick()
        assert painter.calls == ["show", "hide"]
        ring.activity()
        assert painter.calls == ["show", "hide", "show"]

    def test_refusal_stays_dark_across_ticks_until_the_next_action(
        self, ring: GatedOverlay, painter: Painter
    ) -> None:
        ring.begin_session("a")
        ring.notify_blocked()
        ring.tick()
        assert painter.calls == ["show", "hide"]
        ring.activity()
        assert painter.calls == ["show", "hide", "show"]


class TestSafety:
    def test_grab_brackets_the_painter(self, ring: GatedOverlay, painter: Painter) -> None:
        with ring.grab_hidden():
            painter.calls.append("<grab>")
        assert painter.calls == ["grab_begin", "<grab>", "grab_end"]

    def test_a_failing_painter_never_raises(self, world: World) -> None:
        class Broken(Painter):
            def activity(self) -> None:
                raise RuntimeError("window server gone")

        ring = GatedOverlay(Broken(), mode=lambda: world.mode, stopped=lambda: False, watch=False)
        ring.activity()
        ring.tick()

    def test_get_overlay_wraps_the_painter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tst_cu_mcp.overlay as overlay_pkg

        monkeypatch.setenv(OVERLAY_ENV, "1")
        monkeypatch.setattr(overlay_pkg, "_resolve_painter", Painter)
        reset_overlay()
        resolved = get_overlay()
        assert isinstance(resolved, GatedOverlay)
        assert isinstance(resolved.painter, Painter)


class TestPolicyMode:
    def test_missing_file_uses_the_installed_config(self, tmp_path: Path) -> None:
        safety.set_config(Config(mode="full_control"))
        try:
            assert PolicyMode(lambda: tmp_path / "absent.yaml")() == "full_control"
        finally:
            safety.set_config(None)

    def test_reads_the_file_and_follows_edits(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        save_config(Config(mode="full_control"), path)
        mode = PolicyMode(lambda: path)
        assert mode() == "full_control"
        save_config(Config(mode="background", allowed_apps=("Finder",)), path)
        assert mode() == "background"

    def test_a_broken_file_reads_as_background(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("mode: sideways\n", encoding="utf-8")
        assert PolicyMode(lambda: path)() == "background"


class TestHelperParsing:
    """The AppKit helper's pure parts run anywhere."""

    def test_display_id_argument(self) -> None:
        assert _parse_display_id("51") == 51
        assert _parse_display_id(" 0 ") == 0
        assert _parse_display_id("-1") is None
        assert _parse_display_id("main") is None

    def test_lit_keys_prefers_the_target_then_the_primary(self) -> None:
        keys = [(52, "A", 0, 0, 1, 1), (51, "B", 1, 0, 1, 1)]
        assert lit_keys(keys, 51) == [keys[1]]
        assert lit_keys(keys, 99) == [keys[0]]
        assert lit_keys(keys, None) == [keys[0]]
        assert lit_keys([], 51) == []

    def test_reader_splits_arguments_and_acks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("target 51\nshow\n"))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        commands: queue.Queue[tuple[str, str, object]] = queue.Queue()
        _read_stdin(commands)  # type: ignore[arg-type]
        got = [commands.get_nowait()[:2] for _ in range(commands.qsize())]
        assert got == [("target", "51"), ("show", ""), ("quit", "")]
        assert out.getvalue().split() == ["ok", "ok"]
