"""Playwright persistent-profile browser (TD-1710).

Launched only when ``computer_use.browser`` is ``playwright`` and the
Playwright package is importable. Chromium uses a pipe, not a TCP debug
port. The profile lives under the user data dir, never the workspace.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from .protocol import BrowserError

_DEFAULT_TIMEOUT_MS = 30_000


def playwright_available() -> bool:
    """True when the Playwright package can be imported. Does not launch Chrome."""
    try:
        import playwright  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


class PlaywrightBrowserDriver:
    """Live Chromium via a persistent profile. Lazy — first action launches."""

    def __init__(self, profile_dir: Path) -> None:
        self._profile_dir = profile_dir
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    async def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.async_api import async_playwright  # type: ignore[import-not-found]
        except ImportError as exc:
            raise BrowserError(
                "driver_crash",
                "Playwright is not installed; browser computer-use is mock-only.",
            ) from exc
        try:
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self._profile_dir),
                headless=True,
                handle_sigint=False,
                handle_sigterm=False,
                handle_sighup=False,
            )
            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = await self._context.new_page()
            self._page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
        except BrowserError:
            raise
        except Exception as exc:
            await self.aclose()
            raise BrowserError("driver_crash", f"browser driver failed to start: {exc}") from exc
        return self._page

    def _map_error(self, exc: BaseException) -> BrowserError:
        name = type(exc).__name__
        message = str(exc) or name
        if name in {"TimeoutError", "PlaywrightTimeout"} or "Timeout" in name:
            return BrowserError("page_stalled", f"page did not become ready in time: {message}")
        return BrowserError("driver_crash", f"browser driver crashed: {message}")

    async def navigate(self, url: str) -> dict[str, Any]:
        page = await self._ensure_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            title = await page.title()
            return {"url": page.url, "title": title}
        except BrowserError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

    async def click(self, x: float, y: float, button: str = "left") -> dict[str, Any]:
        page = await self._ensure_page()
        try:
            await page.mouse.click(x, y, button=button)
            return {"clicked": {"x": x, "y": y}, "button": button}
        except BrowserError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

    async def type_text(self, text: str) -> dict[str, Any]:
        page = await self._ensure_page()
        try:
            await page.keyboard.type(text)
            return {"typed_chars": len(text)}
        except BrowserError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

    async def scroll(self, dx: int = 0, dy: int = 0) -> dict[str, Any]:
        page = await self._ensure_page()
        try:
            await page.mouse.wheel(dx, dy)
            return {"scrolled": {"dx": dx, "dy": dy}}
        except BrowserError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

    async def screenshot_png(self) -> bytes:
        page = await self._ensure_page()
        try:
            raw = await page.screenshot(type="png")
        except BrowserError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc
        if not isinstance(raw, (bytes, bytearray)):
            raise BrowserError("driver_crash", "screenshot did not return PNG bytes")
        return bytes(raw)

    async def wait(self, timeout_ms: int = 1000, selector: str | None = None) -> dict[str, Any]:
        page = await self._ensure_page()
        try:
            if selector:
                await page.wait_for_selector(selector, timeout=timeout_ms)
            else:
                await page.wait_for_timeout(timeout_ms)
            return {"waited_ms": timeout_ms, "selector": selector}
        except BrowserError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

    async def aclose(self) -> None:
        context, playwright = self._context, self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()
