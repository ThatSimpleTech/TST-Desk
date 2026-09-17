"""browser_pick tests (TD-711).

Act-by-description: code extracts candidates, the judgment picks, the
click lands on the picked element's box center.  Every non-assertion
refuses with fallback guidance — never a hard block.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_dispatch import attach_auto_approver
from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClass,
    DecisionClassifier,
    ScriptedJudgmentBackend,
    ideal_judgment,
)
from tstd.browser import MockBrowserDriver
from tstd.policy import PolicyConfig
from tstd.tools import ToolDispatcher, create_registry
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
        self.id = "sess-pick"
        self.persist_dir = persist
        self.event_log = _Log()
        self.judgment_backend: object = None
        self.judgment_threshold = 0.6


async def _worker(prompt: str) -> str:
    return "B"


def _dispatcher(
    workspace: Path,
    driver: MockBrowserDriver,
    backend: ScriptedJudgmentBackend | None,
    *,
    candidate_selection: bool = True,
) -> tuple[ToolDispatcher, _Session]:
    dispatcher = ToolDispatcher(
        create_registry(candidate_selection=candidate_selection),
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(Boundary(workspace_root=workspace)),
            call_worker=_worker,
        ),
        path_guard=PathGuard(Boundary(workspace_root=workspace)),
        policy=PolicyConfig(),
        workspace=workspace,
    )
    register_builtin_handlers(dispatcher, browser_driver=driver)
    attach_auto_approver(dispatcher)
    session = _Session(workspace / "sess")
    session.persist_dir.mkdir(parents=True, exist_ok=True)
    session.judgment_backend = backend
    return dispatcher, session


class TestBrowserPick:
    async def test_picks_and_clicks_the_box_center(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver()
        backend = ScriptedJudgmentBackend([ideal_judgment("1")])
        dispatcher, session = _dispatcher(tmp_path, driver, backend)
        result = await dispatcher.dispatch(
            "c1", "browser_pick", {"target": "the sign-in button"}, session=session
        )
        assert result.status == "success"
        body = json.loads(result.output)
        assert body["picked"]["name"] == "Sign in"
        # Sign in box: x=200, y=100, 90x24 → center (245, 112).
        assert body["clicked"] == {"x": 245.0, "y": 112.0}
        click = [c for c in driver.calls if c[0] == "click"]
        assert click and click[0][1]["x"] == 245.0

    async def test_actuation_classifies_b(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver()
        backend = ScriptedJudgmentBackend([ideal_judgment("1")])
        dispatcher, session = _dispatcher(tmp_path, driver, backend)
        result = await dispatcher.dispatch(
            "c1", "browser_pick", {"target": "sign in"}, session=session
        )
        assert result.decision_class is DecisionClass.B

    async def test_no_match_refuses_with_fallback_guidance(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver()
        backend = ScriptedJudgmentBackend([ideal_judgment("none")])
        dispatcher, session = _dispatcher(tmp_path, driver, backend)
        result = await dispatcher.dispatch(
            "c1", "browser_pick", {"target": "a missing thing"}, session=session
        )
        assert result.status == "error"
        assert result.error_code == "no_match"
        assert "browser_screenshot" in result.output
        assert [c for c in driver.calls if c[0] == "click"] == []

    async def test_no_backend_refuses(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver()
        dispatcher, session = _dispatcher(tmp_path, driver, None)
        result = await dispatcher.dispatch(
            "c1", "browser_pick", {"target": "sign in"}, session=session
        )
        assert result.status == "error"
        assert result.error_code == "no_backend"

    async def test_low_confidence_is_no_match(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver()
        backend = ScriptedJudgmentBackend([ideal_judgment("1", 0.3)])
        dispatcher, session = _dispatcher(tmp_path, driver, backend)
        result = await dispatcher.dispatch(
            "c1", "browser_pick", {"target": "sign in"}, session=session
        )
        assert result.error_code == "no_match"

    async def test_no_candidates_refuses(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver(candidates=[])
        backend = ScriptedJudgmentBackend([ideal_judgment("0")])
        dispatcher, session = _dispatcher(tmp_path, driver, backend)
        result = await dispatcher.dispatch(
            "c1", "browser_pick", {"target": "anything"}, session=session
        )
        assert result.error_code == "no_candidates"
        assert backend.questions == []  # no judgment spent on an empty page

    async def test_tool_absent_when_flag_off(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver()
        dispatcher, session = _dispatcher(
            tmp_path, driver, None, candidate_selection=False
        )
        result = await dispatcher.dispatch(
            "c1", "browser_pick", {"target": "sign in"}, session=session
        )
        assert result.status == "error"
        assert result.error_code == "unknown_tool"

    async def test_driver_crash_maps_to_refusal(self, tmp_path: Path) -> None:
        driver = MockBrowserDriver(crash=True)
        backend = ScriptedJudgmentBackend([ideal_judgment("1")])
        dispatcher, session = _dispatcher(tmp_path, driver, backend)
        result = await dispatcher.dispatch(
            "c1", "browser_pick", {"target": "sign in"}, session=session
        )
        assert result.status == "error"
        assert result.error_code == "driver_crash"
