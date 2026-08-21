"""Browser computer-use tools on the dispatcher (TD-1710).

No Chrome. The mock is the TD-102 path: scripted pages, a tiny PNG,
crash and stall as ordinary tool_result errors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_dispatch import attach_auto_approver
from tstd.autonomy import AmbiguousClassifier, Boundary, DecisionClass, DecisionClassifier
from tstd.browser import (
    TINY_PNG,
    MockBrowserDriver,
    PlaywrightBrowserDriver,
    browser_driver_from_config,
    playwright_available,
    png_size,
)
from tstd.config import ComputerUseConfig, ModelConfig, Preset, TierConfig
from tstd.policy import ApprovalOutcome, PolicyConfig
from tstd.protocol import ScreenFrame, parse_daemon_event
from tstd.tools import ToolDispatcher, UnclassifiedToolCall, create_registry
from tstd.tools.boundary import PathGuard
from tstd.tools.handlers import register_builtin_handlers


class _Log:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def add(self, event: object) -> object:
        self.events.append(event)
        return event


class _Session:
    def __init__(self, persist: Path) -> None:
        self.id = "sess-browser"
        self.persist_dir = persist
        self.event_log = _Log()


def _config(*, browser: str = "mock") -> ModelConfig:
    tier = TierConfig(
        slug="demo/brain",
        base_url="http://127.0.0.1:11434/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=8192,
        max_output_tokens=256,
    )
    return ModelConfig(
        presets={"demo": Preset(brain=tier, worker=tier, validator=tier)},
        active_preset="demo",
        computer_use=ComputerUseConfig.model_validate({"browser": browser}),
    )


def _dispatcher(
    workspace: Path,
    driver: MockBrowserDriver,
    *,
    approve: bool = True,
    persist: Path | None = None,
) -> tuple[ToolDispatcher, _Session, PathGuard]:
    guard = PathGuard(Boundary(workspace_root=workspace))
    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(Boundary(workspace_root=workspace)),
            call_worker=_worker,
        ),
        path_guard=guard,
        policy=PolicyConfig(),
        workspace=workspace,
    )
    register_builtin_handlers(dispatcher, browser_driver=driver)
    if approve:
        attach_auto_approver(dispatcher)
    session = _Session(persist if persist is not None else workspace / "sess")
    session.persist_dir.mkdir(parents=True, exist_ok=True)
    return dispatcher, session, guard


async def _worker(_prompt: str) -> str:
    return "B"


class TestSixVerbsAndGate:
    async def test_screenshot_is_a_without_approval(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver()
        dispatcher, session, _ = _dispatcher(tmp_path, driver, approve=False)
        result = await dispatcher.dispatch("c1", "browser_screenshot", {}, session=session)
        assert result.status == "success"
        assert result.decision_class is DecisionClass.A
        body = json.loads(result.output)
        assert body["path"].startswith("screens/")
        assert body["path"].endswith(".png")
        assert (session.persist_dir / body["path"]).read_bytes().startswith(b"\x89PNG")
        sidecar = session.persist_dir / body["path"].replace(".png", ".dataurl")
        assert sidecar.read_text(encoding="utf-8").startswith("data:image/png;base64,")
        assert driver.actuations == []
        frames = [e for e in session.event_log.events if isinstance(e, ScreenFrame)]
        assert len(frames) == 1
        assert frames[0].path == body["path"]
        dumped = json.loads(frames[0].model_dump_json())
        assert "png" not in dumped or dumped.get("mime") == "image/png"
        assert "content" not in dumped

    async def test_navigate_is_b_and_needs_approval(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver()
        dispatcher, session, _ = _dispatcher(tmp_path, driver, approve=False)
        with pytest.raises(UnclassifiedToolCall):
            await dispatcher.dispatch(
                "c1", "browser_navigate", {"url": "https://example.com"}, session=session
            )
        assert driver.actuations == []

    async def test_approved_verbs_run_and_refresh_frame(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver(pages={"https://example.com": "Example"})
        dispatcher, session, _ = _dispatcher(tmp_path, driver)
        nav = await dispatcher.dispatch(
            "c1", "browser_navigate", {"url": "https://example.com"}, session=session
        )
        click = await dispatcher.dispatch(
            "c2", "browser_click", {"x": 10, "y": 20}, session=session
        )
        typed = await dispatcher.dispatch("c3", "browser_type", {"text": "hi"}, session=session)
        scroll = await dispatcher.dispatch(
            "c4", "browser_scroll", {"dx": 0, "dy": 40}, session=session
        )
        waited = await dispatcher.dispatch("c5", "browser_wait", {"timeout_ms": 5}, session=session)
        assert {nav.status, click.status, typed.status, scroll.status, waited.status} == {"success"}
        assert nav.decision_class is DecisionClass.B
        assert driver.actuations == ["navigate", "click", "type", "scroll", "wait"]
        frames = [e for e in session.event_log.events if isinstance(e, ScreenFrame)]
        assert len(frames) == 5
        assert json.loads(nav.output)["title"] == "Example"
        assert json.loads(typed.output)["typed_chars"] == 2

    async def test_denied_click_does_not_actuate(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver()
        dispatcher, session, _ = _dispatcher(tmp_path, driver)

        async def _deny(*_args: object) -> ApprovalOutcome:
            return ApprovalOutcome(False, "denied by user")

        dispatcher.approval_handler = _deny
        result = await dispatcher.dispatch("c1", "browser_click", {"x": 1, "y": 2}, session=session)
        assert result.status == "error"
        assert result.error_code == "approval_denied"
        assert driver.actuations == []
        assert session.event_log.events == []

    async def test_path_guard_never_runs(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver()
        dispatcher, session, _ = _dispatcher(tmp_path, driver)

        class Spy(PathGuard):
            def __init__(self) -> None:
                super().__init__(Boundary(workspace_root=tmp_path))
                self.seen: list[str] = []

            def canonicalize(self, raw: str | Path) -> Path:
                self.seen.append(f"canon:{raw}")
                return super().canonicalize(raw)

        spy = Spy()
        dispatcher.path_guard = spy
        await dispatcher.dispatch("c1", "browser_screenshot", {}, session=session)
        await dispatcher.dispatch(
            "c2", "browser_navigate", {"url": "https://example.com"}, session=session
        )
        assert spy.seen == []


class TestFailureModes:
    async def test_driver_crash_is_a_tool_result(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver(crash=True)
        dispatcher, session, _ = _dispatcher(tmp_path, driver)
        result = await dispatcher.dispatch(
            "c1", "browser_navigate", {"url": "https://example.com"}, session=session
        )
        assert result.status == "error"
        assert result.error_code == "driver_crash"
        assert "crashed" in result.output
        assert driver.actuations == []

    async def test_stalled_page_is_a_tool_result(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver(stall=True)
        dispatcher, session, _ = _dispatcher(tmp_path, driver)
        result = await dispatcher.dispatch("c1", "browser_wait", {"timeout_ms": 1}, session=session)
        assert result.status == "error"
        assert result.error_code == "page_stalled"
        assert driver.actuations == []

    def test_screen_frame_round_trips_path_not_bytes(self) -> None:
        event = ScreenFrame(
            session_id="s1",
            path="screens/aa.png",
            mime="image/png",
            width=1,
            height=1,
            seq=1,
        )
        back = parse_daemon_event(event.model_dump_json())
        assert isinstance(back, ScreenFrame)
        assert back.path == "screens/aa.png"
        assert "png_base64" not in event.model_dump()


class TestFactoryAndMock:
    def test_default_and_missing_playwright_are_mock(self, tmp_path: Path) -> None:
        mock = browser_driver_from_config(_config(browser="mock"), tmp_path)
        live = browser_driver_from_config(_config(browser="playwright"), tmp_path)
        assert isinstance(mock, MockBrowserDriver)
        if playwright_available():
            assert isinstance(live, PlaywrightBrowserDriver)
        else:
            assert isinstance(live, MockBrowserDriver)

    def test_shipped_browser_default_is_mock(self) -> None:
        assert ComputerUseConfig().browser == "mock"

    async def test_tiny_png_is_real_png(self) -> None:
        assert TINY_PNG.startswith(b"\x89PNG")
        assert png_size(TINY_PNG) == (1, 1)
        driver = MockBrowserDriver()
        raw = await driver.screenshot_png()
        assert raw == TINY_PNG

    def test_browser_package_does_not_bind(self) -> None:
        root = Path(__file__).resolve().parents[1] / "tstd" / "browser"
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "0.0.0.0" not in text
            assert "bind(" not in text
