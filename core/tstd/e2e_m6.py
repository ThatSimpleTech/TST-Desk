"""M6 exit harness (TD-3405).

Mock desktop: Class A screenshot, focus-mismatch click (no actuation),
approved click — all through the dispatcher. CI's TD-1710 path is the
mock six verbs + ``screen_frame``. Playwright is ``@pytest.mark.live``
only and is not claimed unless Chromium started. Not ``e2e_harness.run``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from .browser import (
    BrowserDriver,
    MockBrowserDriver,
    PlaywrightBrowserDriver,
    playwright_available,
)
from .config import ComputerUseConfig, EmbeddingsConfig, ModelConfig, Preset, TierConfig
from .daemon import Daemon
from .desktop import DesktopDriver, MockDesktopDriver
from .e2e_checks import HarnessResult, _check
from .e2e_harness import _send, _wait_for_port_file
from .e2e_m5 import _hello, _recv_event
from .mock import MockProvider, Script

_STEERING = "# M6 harness\n\nKeep replies short.\n"
_DESK_PROMPT = "m6 desktop computer-use"
_BROWSER_PROMPT = "m6 browser computer-use"
_REPLY = "m6 mock reply"
_BRAIN = "m6-brain"
_TURN_TIMEOUT = 30.0
_LIVE_TIMEOUT = 45.0
_BUDGET = 60.0
_REFUSE_ARGS = {"x": 4.0, "y": 5.0, "expect_window": "Chrome"}
_CLICK_ARGS = {"x": 10.0, "y": 20.0}
_BROWSER_STEPS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("browser_screenshot", {}),
    ("browser_navigate", {"url": "about:blank"}),
    ("browser_click", {"x": 1.0, "y": 1.0}),
    ("browser_type", {"text": "x"}),
    ("browser_scroll", {"dx": 0, "dy": 1}),
    ("browser_wait", {"timeout_ms": 5}),
)


def have_display() -> bool:
    """Whether this OS can plausibly host a real browser window."""
    if sys.platform in {"darwin", "win32"}:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _tier(slug: str) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url="http://127.0.0.1:9/v1",
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=8_000,
        max_output_tokens=1_000,
    )


def m6_config() -> ModelConfig:
    """Known slugs so the mock sequence is keyed without discovery."""
    return ModelConfig(
        presets={
            "m6-harness": Preset(brain=_tier(_BRAIN), worker=_tier("m6-w"), validator=_tier("m6-v"))
        },
        active_preset="m6-harness",
        embeddings=EmbeddingsConfig(base_url="", model=""),
        computer_use=ComputerUseConfig(),
    )


class _SequentialMock(MockProvider):
    """Play scripts in order on any slug — lead-turns hand off to worker."""

    def __init__(self, steps: list[Script]) -> None:
        super().__init__(default=Script(kind="stream", content="(unused)"))
        self._steps = steps
        self._pos = 0

    def _script_for(self, model: str) -> Script:
        if self._pos < len(self._steps):
            script = self._steps[self._pos]
            self._pos += 1
            return script
        return self._default


def _provider(steps: list[tuple[str, dict[str, Any]]]) -> MockProvider:
    scripts = [
        Script(kind="tool_call", tool_name=name, tool_arguments=json.dumps(args))
        for name, args in steps
    ]
    scripts.append(Script(kind="stream", content=_REPLY))
    return _SequentialMock(scripts)


async def _wait_turn(
    ws: Any, prompt: str, timeout_secs: float
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    events: list[dict[str, Any]] = []
    seen_user = False
    deadline = time.monotonic() + timeout_secs
    session_id = ""
    while time.monotonic() < deadline:
        event = await _recv_event(ws, max(0.1, deadline - time.monotonic()))
        events.append(event)
        if event.get("type") == "error":
            return events, event
        if event.get("type") == "user_turn" and event.get("content") == prompt:
            seen_user = True
        if event.get("type") == "approval_request":
            session_id = str(event.get("session_id") or session_id)
            await _send(
                ws,
                {
                    "type": "approve",
                    "session_id": session_id,
                    "tool_call_id": event["tool_call_id"],
                },
            )
        if event.get("type") == "turn_complete" and seen_user:
            return events, None
    return events, None


def _pairs(events: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Match each tool_call to the next tool_result. Mock IDs may collide."""
    last: dict[str, Any] | None = None
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for event in events:
        if event.get("type") == "tool_call":
            last = event
        elif event.get("type") == "tool_result" and last is not None:
            pairs.append((last, event))
            last = None
    return pairs


def _named(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], name: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [pair for pair in pairs if pair[0].get("name") == name]


def _prepare_workspace(workspace: Path, data_dir: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(_STEERING, encoding="utf-8")


async def _run_session(
    workspace: Path,
    data_dir: Path,
    provider: MockProvider,
    prompt: str,
    *,
    desktop: DesktopDriver,
    browser: BrowserDriver,
    timeout_secs: float,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    _prepare_workspace(workspace, data_dir)
    daemon = Daemon(data_dir=data_dir, provider=provider)
    daemon.config = m6_config()
    daemon.desktop_driver = desktop
    daemon.browser_driver = browser
    daemon_task = asyncio.create_task(daemon.run())
    events: list[dict[str, Any]] = []
    error: dict[str, Any] | None = None
    try:
        info = await _wait_for_port_file(data_dir)
        async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
            await _hello(ws, str(info["token"]))
            await _send(ws, {"type": "open_workspace", "path": str(workspace)})
            opened = await _recv_event(ws, 15.0)
            events.append(opened)
            session_id = str(opened.get("session_id") or "")
            await _send(ws, {"type": "attach", "session_id": session_id, "from_seq": 1})
            await _send(ws, {"type": "user_message", "session_id": session_id, "content": prompt})
            turn_events, error = await _wait_turn(ws, prompt, timeout_secs)
            events.extend(turn_events)
            await ws.close()
    finally:
        if not daemon_task.done():
            daemon._shutdown_event.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(daemon_task, timeout=10.0)
            if not daemon_task.done():
                daemon_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await daemon_task
    return events, error


def _verify_desktop(
    result: HarnessResult,
    events: list[dict[str, Any]],
    error: dict[str, Any] | None,
    desktop: MockDesktopDriver,
) -> None:
    pairs = _pairs(events)
    shots, clicks = _named(pairs, "desktop_screenshot"), _named(pairs, "desktop_click")
    approvals = [e for e in events if e.get("type") == "approval_request"]
    complete = next((e for e in events if e.get("type") == "turn_complete"), None)
    shot = shots[0] if shots else None
    refuse, approve = (clicks[0] if clicks else None), (clicks[1] if len(clicks) > 1 else None)
    mismatch_recorded = any(
        name == "click" and kwargs.get("expect_window") == "Chrome"
        for name, kwargs in desktop.calls
    )
    _check(
        result,
        "screenshot class A",
        shot is not None
        and shot[0].get("decision_class") == "A"
        and shot[1].get("status") == "success",
        f"n={len(shots)}",
    )
    _check(
        result,
        "refused click focus_mismatch",
        refuse is not None
        and refuse[0].get("decision_class") == "B"
        and (refuse[0].get("arguments") or {}).get("expect_window") == "Chrome"
        and refuse[1].get("error_code") == "focus_mismatch",
        f"n={len(clicks)}",
    )
    _check(
        result,
        "approved click class B",
        approve is not None
        and approve[0].get("decision_class") == "B"
        and approve[1].get("status") == "success",
        f"n={len(clicks)}",
    )
    _check(
        result,
        "refuse did not actuate",
        not mismatch_recorded and desktop.actuations == ["click"],
        f"actuations={desktop.actuations!r}",
    )
    classified = all(call.get("decision_class") in {"A", "B"} for call, _ in pairs)
    _check(
        result,
        "clicks through classifier",
        classified and len(approvals) == 2 and error is None,
        f"approvals={len(approvals)}",
    )
    _check(
        result,
        "desktop turn complete",
        complete is not None and not complete.get("failed"),
        f"type={None if complete is None else complete.get('type')}",
    )


def _verify_browser(
    result: HarnessResult,
    events: list[dict[str, Any]],
    error: dict[str, Any] | None,
    browser: BrowserDriver,
    *,
    live: bool,
) -> None:
    pairs = _pairs(events)
    names = [call.get("name") for call, _ in pairs]
    expected = [name for name, _ in _BROWSER_STEPS]
    frames = [e for e in events if e.get("type") == "screen_frame"]
    statuses = [item.get("status") for _, item in pairs]
    classes = [call.get("decision_class") for call, _ in pairs]
    complete = next((e for e in events if e.get("type") == "turn_complete"), None)
    launched = getattr(browser, "_page", None) is not None
    actuations = getattr(browser, "actuations", None)
    _check(result, "six browser verbs", names == expected, f"names={names}")
    _check(
        result,
        "browser classes A then B",
        classes == ["A"] + ["B"] * 5 and statuses == ["success"] * 6,
        f"classes={classes} statuses={statuses}",
    )
    _check(result, "screen_frame", bool(frames), f"n={len(frames)}")
    if live:
        _check(
            result,
            "live browser launched",
            launched and isinstance(browser, PlaywrightBrowserDriver),
            f"driver={type(browser).__name__} page={launched}",
        )
    else:
        _check(
            result,
            "browser path (mock)",
            isinstance(browser, MockBrowserDriver)
            and actuations == ["navigate", "click", "type", "scroll", "wait"],
            f"driver={type(browser).__name__} actuations={actuations!r}",
        )
    _check(
        result,
        "browser turn complete",
        complete is not None and not complete.get("failed") and error is None,
        f"failed={None if complete is None else complete.get('failed')}",
    )


async def run_m6(workspace: Path, data_dir: Path) -> HarnessResult:
    """Drive the M6 exit: mock desktop CU plus mock TD-1710 browser path."""
    started = time.monotonic()
    desktop = MockDesktopDriver(foreground_title="Terminal", foreground_app="zsh")
    browser = MockBrowserDriver()
    desk_events, desk_error = await _run_session(
        workspace / "desktop",
        data_dir / "desktop",
        _provider(
            [
                ("desktop_screenshot", {}),
                ("desktop_click", _REFUSE_ARGS),
                ("desktop_click", _CLICK_ARGS),
            ]
        ),
        _DESK_PROMPT,
        desktop=desktop,
        browser=MockBrowserDriver(),
        timeout_secs=_TURN_TIMEOUT,
    )
    browse_events, browse_error = await _run_session(
        workspace / "browser",
        data_dir / "browser",
        _provider(list(_BROWSER_STEPS)),
        _BROWSER_PROMPT,
        desktop=MockDesktopDriver(),
        browser=browser,
        timeout_secs=_TURN_TIMEOUT,
    )
    result = HarnessResult(elapsed=time.monotonic() - started)
    result.notes.append("TD-1710 CI green is the mock six-verb path, not Playwright")
    _verify_desktop(result, desk_events, desk_error, desktop)
    _verify_browser(result, browse_events, browse_error, browser, live=False)
    _check(result, "under budget", result.elapsed < _BUDGET, f"{result.elapsed:.1f}s")
    _check(result, "m6 exit criterion", result.ok, "mock desktop + mock browser")
    return result


async def run_m6_browser_live(workspace: Path, data_dir: Path) -> HarnessResult:
    """Six TD-1710 verbs on Playwright. Does not claim live unless it launched."""
    started = time.monotonic()
    result = HarnessResult(elapsed=0.0)
    if not playwright_available() or not have_display():
        reason = "Playwright is not installed" if not playwright_available() else "no display"
        result.elapsed = time.monotonic() - started
        _check(result, "live browser launched", False, reason)
        return result
    browser = PlaywrightBrowserDriver(data_dir / "browser-profile")
    events, error = await _run_session(
        workspace,
        data_dir,
        _provider(list(_BROWSER_STEPS)),
        _BROWSER_PROMPT,
        desktop=MockDesktopDriver(),
        browser=browser,
        timeout_secs=_LIVE_TIMEOUT,
    )
    result.elapsed = time.monotonic() - started
    _verify_browser(result, events, error, browser, live=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M6 exit harness — mock CU pass (TD-3405)")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    scratch = tempfile.TemporaryDirectory(prefix="tstd-e2e-m6-")
    root = Path(scratch.name)
    workspace = args.workspace or root / "workspace"
    data_dir = args.data_dir or root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_m6(workspace, data_dir))
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
