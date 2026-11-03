"""Agent cursors (TD-5227): identity, colors, the helper's motion, the wire.

No AppKit here: the helper's motion is a plain model, and the painter's
line protocol is checked against a recording transport.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tst_cu_mcp import ui
from tst_cu_mcp.overlay import set_overlay
from tst_cu_mcp.overlay.agents import (
    PALETTE,
    Agent,
    AgentBook,
    SlotRegistry,
    clean_key,
    clean_label,
    label_for_client,
    set_client_name,
)
from tst_cu_mcp.overlay.darwin import DarwinOverlay
from tst_cu_mcp.overlay.darwin_cursor import (
    CURSOR_IDLE_SECONDS,
    TIP_X,
    TIP_Y,
    CursorModel,
    panel_origin,
    parse_cursor_args,
    parse_hex,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="flock slots are POSIX")


class TestIdentity:
    @pytest.mark.parametrize(
        "name, label",
        [
            ("claude-ai", "Claude"),
            ("Grok Build", "Grok"),
            ("tstd", "TST Desk"),
            ("my_custom-client", "My Custom Client"),
            ("", "Agent"),
        ],
    )
    def test_client_names_read_as_labels(self, name: str, label: str) -> None:
        assert label_for_client(name) == label

    def test_keys_and_labels_survive_the_line_protocol(self) -> None:
        assert clean_key("a b\nc;d-1_2") == "abcd-1_2"
        assert clean_key("   ") == "client"
        assert clean_label("  two\n lines ") == "two lines"
        assert len(clean_label("x" * 100)) == 40

    def test_unnamed_agents_carry_the_clients_label(self, tmp_path: Path) -> None:
        book = AgentBook(SlotRegistry(tmp_path))
        set_client_name("claude-ai")
        try:
            assert book.current().label == "Claude"
            assert book.use("s1", "Fix the login").label == "Fix the login"
            assert book.use("s1").label == "Fix the login"
        finally:
            set_client_name("Agent")


@posix_only
class TestSlots:
    def test_live_agents_never_share_a_color(self, tmp_path: Path) -> None:
        registry = SlotRegistry(tmp_path)
        slots = [registry.claim(f"agent-{n}") for n in range(len(PALETTE))]
        assert sorted(slots) == list(range(len(PALETTE)))
        assert registry.claim("agent-3") == slots[3]

    def test_a_released_slot_is_reused(self, tmp_path: Path) -> None:
        registry = SlotRegistry(tmp_path)
        first = registry.claim("a")
        registry.claim("b")
        registry.release("a")
        assert registry.claim("c") == first

    def test_another_process_holding_a_slot_moves_us_on(self, tmp_path: Path) -> None:
        """The registry is machine-wide: a second client gets the next color."""
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import sys
                    from pathlib import Path
                    from tst_cu_mcp.overlay.agents import SlotRegistry
                    r = SlotRegistry(Path({str(tmp_path)!r}))
                    print(r.claim("other"), flush=True)
                    sys.stdin.read()
                    """
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ},
        )
        try:
            assert holder.stdout is not None
            held = int(holder.stdout.readline())
            assert SlotRegistry(tmp_path).claim("mine") != held
        finally:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=10)
        # Its lock went with it: the slot is free again.
        assert SlotRegistry(tmp_path).claim("later") == held

    def test_past_the_palette_colors_repeat_without_failing(self, tmp_path: Path) -> None:
        registry = SlotRegistry(tmp_path, size=2)
        registry.claim("a")
        registry.claim("b")
        assert registry.claim("c") in (0, 1)


class TestCursorModel:
    def test_new_cursor_starts_at_its_point_and_glides_to_the_next(self) -> None:
        model = CursorModel()
        model.place("a", 100, 100, "#2F6FDE", "A", False, now=0.0)
        model.step(now=0.0, dt=0.1, reduce_motion=False)
        state = model.cursors["a"]
        assert (state.x, state.y) == (100, 100)
        model.place("a", 200, 100, "#2F6FDE", "A", False, now=0.1)
        model.step(now=0.13, dt=0.033, reduce_motion=False)
        assert 100 < state.x < 200
        for tick in range(30):
            model.step(now=0.2 + tick * 0.033, dt=0.033, reduce_motion=False)
        assert state.x == pytest.approx(200, abs=0.5)

    def test_reduce_motion_jumps_and_never_ripples(self) -> None:
        model = CursorModel()
        model.place("a", 100, 100, "#2F6FDE", "A", False, now=0.0)
        model.step(now=0.0, dt=0.1, reduce_motion=True)
        model.place("a", 300, 50, "#2F6FDE", "A", True, now=0.1)
        model.step(now=0.13, dt=0.033, reduce_motion=True)
        state = model.cursors["a"]
        assert (state.x, state.y) == (300, 50)
        assert state.ripple == 0.0

    def test_a_click_ripples_then_settles(self) -> None:
        model = CursorModel()
        model.place("a", 10, 10, "#2F6FDE", "A", True, now=0.0)
        model.step(now=0.03, dt=0.03, reduce_motion=False)
        assert 0 < model.cursors["a"].ripple < 1
        model.step(now=1.0, dt=1.0, reduce_motion=False)
        assert model.cursors["a"].ripple == 0.0

    def test_an_idle_agent_fades_out(self) -> None:
        model = CursorModel()
        model.place("a", 10, 10, "#2F6FDE", "A", False, now=0.0)
        model.step(now=0.5, dt=0.5, reduce_motion=False)
        assert model.cursors["a"].alpha > 0.9
        now = 0.5
        while now < CURSOR_IDLE_SECONDS + 3.0:
            now += 0.1
            model.step(now=now, dt=0.1, reduce_motion=False)
        assert model.cursors["a"].alpha == 0.0

    def test_hide_is_instant_and_per_agent(self) -> None:
        model = CursorModel()
        model.place("a", 10, 10, "#2F6FDE", "A", False, now=0.0)
        model.place("b", 20, 20, "#16A34A", "B", False, now=0.0)
        model.step(now=0.5, dt=0.5, reduce_motion=False)
        model.hide("a")
        assert model.cursors["a"].alpha == 0.0
        assert model.cursors["b"].alpha > 0.9
        model.hide(None)
        assert model.cursors["b"].alpha == 0.0


class TestWire:
    def test_cursor_arguments_parse_and_reject_garbage(self) -> None:
        assert parse_cursor_args("s1 10.5 -20 #2F6FDE 1 Fix the login") == (
            "s1",
            10.5,
            -20.0,
            "#2F6FDE",
            True,
            "Fix the login",
        )
        assert parse_cursor_args("s1 10 20 #2F6FDE 0")[-1] == "Agent"  # type: ignore[index]
        assert parse_cursor_args("s1 ten 20 #2F6FDE 0 x") is None
        assert parse_cursor_args("s1 nan 20 #2F6FDE 0 x") is None
        assert parse_cursor_args("s1 10 20 blue 0 x") is None
        assert parse_cursor_args("s1 10 20") is None

    def test_hex_colors(self) -> None:
        assert parse_hex("#FF0080") == (1.0, 0.0, 128 / 255)
        assert parse_hex("FF0080") is None
        assert parse_hex("#GG0000") is None

    def test_panel_tip_lands_on_the_point(self) -> None:
        # Cocoa y grows up from the primary display's bottom edge.
        x, y = panel_origin(500, 200, primary_height=1080)
        assert x + TIP_X == 500
        assert 1080 - (y + 72 - TIP_Y) == 200

    def test_darwin_painter_sends_one_line_per_cursor(self) -> None:
        sent: list[str] = []

        class Transport:
            def send(self, command: str, *, timeout: float = 1.0) -> bool:
                sent.append(command)
                return True

            def close(self) -> None:
                return None

        painter = DarwinOverlay(spawn=Transport)
        painter.cursor(Agent("s 1", "Fix\nlogin", "#2F6FDE"), 10, 20.25, True)
        painter.cursor_hide("s1")
        painter.cursor_hide(None)
        assert sent == [
            "hide",  # the start-up probe (TD-5230)
            "cursor s1 10.0 20.2 #2F6FDE 1 Fix login",
            "cursor_hide s1",
            "cursor_hide *",
        ]


class TestElementPoint:
    """Background actions put the cursor on the element they acted on."""

    class Recorder:
        def __init__(self) -> None:
            self.points: list[tuple[float, float, bool]] = []
            self.targets: list[int | None] = []

        def pointer(self, x: float, y: float, *, click: bool = False) -> None:
            self.points.append((x, y, click))

        def set_target(self, display_id: int | None) -> None:
            self.targets.append(display_id)

    def test_frame_center_with_a_click(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tst_cu_mcp import input_control
        from tst_cu_mcp.displays import DisplayInfo

        displays = [
            DisplayInfo(
                display_id=52, index=0, x=0, y=0, width=1920, height=1080, scale=1.0, is_main=True
            ),
            DisplayInfo(
                display_id=53,
                index=1,
                x=-1920,
                y=0,
                width=1920,
                height=1080,
                scale=1.0,
                is_main=False,
            ),
        ]
        monkeypatch.setattr(input_control, "list_displays", lambda: displays)
        recorder = self.Recorder()
        set_overlay(recorder)  # type: ignore[arg-type]
        ui._point_at({"x": -1506, "y": 734, "width": 48, "height": 48})
        assert recorder.points == [(-1482.0, 758.0, True)]
        # TD-5232: the ring moves to the element's display, as for a click.
        assert recorder.targets == [53]

    @pytest.mark.parametrize("frame", [None, {}, {"x": 1}, {"x": "a", "y": 1, "width": 1}])
    def test_no_usable_frame_moves_nothing(self, frame: Any) -> None:
        recorder = self.Recorder()
        set_overlay(recorder)  # type: ignore[arg-type]
        ui._point_at(frame)
        assert recorder.points == []


def test_the_handshake_names_the_client() -> None:
    from mcp.shared.message import SessionMessage
    from mcp_types import JSONRPCRequest

    from tst_cu_mcp.overlay.agents import client_label
    from tst_cu_mcp.stdio_transport import _note_client

    try:
        _note_client(
            SessionMessage(
                JSONRPCRequest(
                    jsonrpc="2.0",
                    id=1,
                    method="initialize",
                    params={"clientInfo": {"name": "grok", "version": "1"}},
                )
            )
        )
        assert client_label() == "Grok"
        _note_client("not a message")
    finally:
        set_client_name("Agent")
