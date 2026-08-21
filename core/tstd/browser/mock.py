"""In-process browser driver for CI (TD-102 / TD-1710).

Scripted pages and a tiny PNG. It never launches Chrome.
"""

from __future__ import annotations

from typing import Any

from .protocol import TINY_PNG, BrowserError, scripted_hit_node


class MockBrowserDriver:
    """The TD-102 mock path: observe, never start a real browser."""

    def __init__(
        self,
        *,
        pages: dict[str, str] | None = None,
        crash: bool = False,
        stall: bool = False,
    ) -> None:
        self.pages = dict(pages) if pages is not None else {"about:blank": "Blank"}
        self.url = "about:blank"
        self.crash = crash
        self.stall = stall
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.actuations: list[str] = []

    def _guard(self, *, actuating: bool) -> None:
        if self.crash:
            raise BrowserError("driver_crash", "browser driver crashed")
        if actuating and self.stall:
            raise BrowserError("page_stalled", "page did not become ready in time")

    def _record(self, name: str, actuating: bool, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        if actuating:
            self.actuations.append(name)

    async def navigate(self, url: str) -> dict[str, Any]:
        self._guard(actuating=True)
        self._record("navigate", True, url=url)
        self.url = url
        title = self.pages.get(url, url)
        return {"url": url, "title": title}

    async def click(self, x: float, y: float, button: str = "left") -> dict[str, Any]:
        self._guard(actuating=True)
        self._record("click", True, x=x, y=y, button=button)
        return {"clicked": {"x": x, "y": y}, "button": button}

    async def type_text(self, text: str) -> dict[str, Any]:
        self._guard(actuating=True)
        self._record("type", True, chars=len(text))
        return {"typed_chars": len(text)}

    async def scroll(self, dx: int = 0, dy: int = 0) -> dict[str, Any]:
        self._guard(actuating=True)
        self._record("scroll", True, dx=dx, dy=dy)
        return {"scrolled": {"dx": dx, "dy": dy}}

    async def screenshot_png(self) -> bytes:
        # Capture is not actuation: a stalled page can still be photographed.
        if self.crash:
            raise BrowserError("driver_crash", "browser driver crashed")
        self._record("screenshot", False)
        return TINY_PNG

    async def wait(self, timeout_ms: int = 1000, selector: str | None = None) -> dict[str, Any]:
        self._guard(actuating=True)
        self._record("wait", True, timeout_ms=timeout_ms, selector=selector)
        return {"waited_ms": timeout_ms, "selector": selector}

    async def hit_test(self, x: float, y: float) -> dict[str, Any]:
        # Observe only: a stalled page can still be inspected.
        if self.crash:
            raise BrowserError("driver_crash", "browser driver crashed")
        self._record("hit_test", False, x=x, y=y)
        return scripted_hit_node(x, y)

    async def aclose(self) -> None:
        return None
