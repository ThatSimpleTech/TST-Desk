"""Wayland input strategy: portal RemoteDesktop / libei (TD-4901b).

This module is input *only*. It does not capture. Availability is a
RemoteDesktop listing, not XTEST through XWayland.

libei event injection is not implemented on this checkout — no Wayland
session to live-verify. Tests inject a driver. ``available`` is never
true just because ``DISPLAY`` is set.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tst_cu_mcp.backends.linux import linux_session_kind

MoveFn = Callable[[float, float], None]
ClickFn = Callable[[float, float, str, int], None]
TypeFn = Callable[[str], None]
ScrollFn = Callable[[int, int], None]
CursorFn = Callable[[], tuple[int, int]]
KeysFn = Callable[[str], None]


class WaylandInputError(RuntimeError):
    """RemoteDesktop is listed but this host cannot inject input yet."""


@dataclass
class WaylandInput:
    """RemoteDesktop-shaped input. libei injection is injected or refused."""

    move: MoveFn | None = None
    click: ClickFn | None = None
    type_text: TypeFn | None = None
    scroll: ScrollFn | None = None
    cursor: CursorFn | None = None
    press_keys: KeysFn | None = None
    actuations: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def available(self) -> bool:
        """True when this is Wayland and an injector is actually wired.

        A portal listing is not enough. Live libei is injected in tests;
        this host has no Wayland session to verify against.
        """
        if linux_session_kind() != "wayland":
            return False
        return self.click is not None or self.move is not None

    def _require(self) -> None:
        if not self.available():
            raise WaylandInputError(
                "Wayland input requires portal RemoteDesktop / libei "
                "(TD-4901b). An XWayland DISPLAY is not a substitute."
            )

    def move_mouse(self, x: float, y: float) -> None:
        self._require()
        if self.move is None:
            raise WaylandInputError(
                "RemoteDesktop is listed but libei injection is not live-"
                "verified on this host. See docs/wayland-computer-use.md."
            )
        self.actuations.append(("move", (x, y)))
        self.move(x, y)

    def click_at(self, x: float, y: float, button: str, count: int) -> None:
        self._require()
        if self.click is None:
            raise WaylandInputError(
                "RemoteDesktop is listed but libei injection is not live-"
                "verified on this host. See docs/wayland-computer-use.md."
            )
        self.actuations.append(("click", (x, y, button, count)))
        self.click(x, y, button, count)

    def type_at(self, text: str) -> None:
        self._require()
        if self.type_text is None:
            raise WaylandInputError(
                "RemoteDesktop is listed but libei injection is not live-"
                "verified on this host. See docs/wayland-computer-use.md."
            )
        self.actuations.append(("type", (len(text),)))
        self.type_text(text)

    def scroll_by(self, dx: int, dy: int) -> None:
        self._require()
        if self.scroll is None:
            raise WaylandInputError(
                "RemoteDesktop is listed but libei injection is not live-"
                "verified on this host. See docs/wayland-computer-use.md."
            )
        self.actuations.append(("scroll", (dx, dy)))
        self.scroll(dx, dy)

    def cursor_position(self) -> tuple[int, int]:
        self._require()
        if self.cursor is None:
            return (0, 0)
        return self.cursor()

    def press(self, combo: str) -> None:
        self._require()
        if self.press_keys is None:
            raise WaylandInputError(
                "RemoteDesktop is listed but libei injection is not live-"
                "verified on this host. See docs/wayland-computer-use.md."
            )
        self.actuations.append(("keys", (combo,)))
        self.press_keys(combo)
