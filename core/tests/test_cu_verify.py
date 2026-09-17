"""Actuation verification tests (TD-709, dev build).

A refuted actuation is annotated so the loop can re-check or retry;
verified and unavailable stay silent.  The disabled path — no verifier
wired — is byte-identical to the pre-TD-709 dispatcher.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_dispatch import attach_auto_approver
from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClassifier,
    ScriptedJudgmentBackend,
    ideal_judgment,
)
from tstd.browser import MockBrowserDriver
from tstd.cu_verify import ActuationVerifier, DriverStateProbe
from tstd.desktop import MockDesktopDriver
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
        self.id = "sess-verify"
        self.persist_dir = persist
        self.event_log = _Log()


def _dispatcher(
    workspace: Path,
    driver: MockDesktopDriver,
    backend: ScriptedJudgmentBackend | None,
) -> ToolDispatcher:
    async def _worker(prompt: str) -> str:
        return "B"

    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(Boundary(workspace_root=workspace)),
            call_worker=_worker,
        ),
        path_guard=PathGuard(Boundary(workspace_root=workspace)),
        policy=PolicyConfig(),
        workspace=workspace,
    )
    register_builtin_handlers(dispatcher, desktop_driver=driver)
    if backend is not None:
        dispatcher.verifier = ActuationVerifier(backend)
    attach_auto_approver(dispatcher)
    return dispatcher


def _session(tmp_path: Path) -> _Session:
    session = _Session(tmp_path / "sess")
    session.persist_dir.mkdir(parents=True, exist_ok=True)
    return session


class TestDispatcherIntegration:
    async def test_refuted_actuation_is_annotated(self, tmp_path: Path) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("no")])
        dispatcher = _dispatcher(tmp_path, MockDesktopDriver(), backend)
        result = await dispatcher.dispatch(
            "c1", "desktop_click", {"x": 10, "y": 10}, session=_session(tmp_path)
        )
        assert result.status == "success"
        assert result.verification == "refuted"
        assert "verification: refuted" in result.output

    async def test_verified_actuation_stays_silent(self, tmp_path: Path) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("yes")])
        dispatcher = _dispatcher(tmp_path, MockDesktopDriver(), backend)
        result = await dispatcher.dispatch(
            "c1", "desktop_click", {"x": 10, "y": 10}, session=_session(tmp_path)
        )
        assert result.status == "success"
        assert result.verification == "verified"
        assert "verification:" not in result.output

    async def test_no_verifier_is_byte_identical(self, tmp_path: Path) -> None:
        dispatcher = _dispatcher(tmp_path, MockDesktopDriver(), None)
        result = await dispatcher.dispatch(
            "c1", "desktop_click", {"x": 10, "y": 10}, session=_session(tmp_path)
        )
        assert result.status == "success"
        assert result.verification is None
        assert "verification:" not in result.output

    async def test_capture_tool_is_never_verified(self, tmp_path: Path) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("no")])
        dispatcher = _dispatcher(tmp_path, MockDesktopDriver(), backend)
        result = await dispatcher.dispatch(
            "c1", "desktop_screenshot", {}, session=_session(tmp_path)
        )
        assert result.status == "success"
        assert result.verification is None
        assert backend.questions == []

    async def test_raising_verifier_fails_open(self, tmp_path: Path) -> None:
        class _Boom:
            async def verify(self, *_args: object, **_kwargs: object) -> object:
                raise RuntimeError("verifier exploded")

        dispatcher = _dispatcher(tmp_path, MockDesktopDriver(), None)
        dispatcher.verifier = _Boom()  # type: ignore[assignment]
        result = await dispatcher.dispatch(
            "c1", "desktop_click", {"x": 10, "y": 10}, session=_session(tmp_path)
        )
        assert result.status == "success"
        assert result.verification is None


class TestActuationVerifier:
    async def test_unavailable_without_any_state(self) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("yes")])
        verifier = ActuationVerifier(backend)
        outcome = await verifier.verify("desktop_click", {}, None, None, "ok")
        assert outcome.status == "unavailable"
        assert backend.questions == []  # no state, no judgment spent

    async def test_low_confidence_is_unavailable(self) -> None:
        verifier = ActuationVerifier(
            ScriptedJudgmentBackend([ideal_judgment("yes", 0.4)]), threshold=0.6
        )
        outcome = await verifier.verify("desktop_click", {}, "before", "after", "ok")
        assert outcome.status == "unavailable"

    async def test_backend_error_is_unavailable(self) -> None:
        class _Boom:
            @property
            def name(self) -> str:
                return "boom"

            async def judge(self, _question: object) -> object:
                raise RuntimeError("down")

        verifier = ActuationVerifier(_Boom())  # type: ignore[arg-type]
        outcome = await verifier.verify("desktop_click", {}, "before", "after", "ok")
        assert outcome.status == "unavailable"

    async def test_state_is_capped(self) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("yes")])
        verifier = ActuationVerifier(backend, max_state_chars=20)
        await verifier.verify("desktop_click", {}, "b" * 500, "a" * 500, "o" * 500)
        question = backend.questions[0]
        for _key, value in question.state:
            assert len(value) <= 20

    async def test_sensitive_arguments_are_redacted(self) -> None:
        """Typed text and URL credentials never reach the judgment payload."""
        backend = ScriptedJudgmentBackend([ideal_judgment("yes")])
        verifier = ActuationVerifier(backend)
        await verifier.verify(
            "desktop_type",
            {"text": "hunter2", "url": "https://user:pass@example.com/path?token=x", "x": 1},
            "before",
            "after",
            "ok",
        )
        question = backend.questions[0]
        arguments = dict(question.state)["Arguments"]
        assert "hunter2" not in arguments
        assert "[7 chars]" in arguments
        assert "example.com" in arguments
        assert "user:pass" not in arguments
        assert "token=x" not in arguments


class TestDriverStateProbe:
    async def test_desktop_mock_state(self) -> None:
        probe = DriverStateProbe(
            MockDesktopDriver(foreground_title="Editor", foreground_app="code"),
            MockBrowserDriver(),
        )
        assert await probe.snapshot("desktop_click") == (
            "foreground_app=code foreground_title=Editor"
        )

    async def test_browser_mock_state(self) -> None:
        probe = DriverStateProbe(MockDesktopDriver(), MockBrowserDriver())
        assert await probe.snapshot("browser_click") == "url=about:blank title=Blank"

    async def test_indescribable_driver_is_none(self) -> None:
        probe = DriverStateProbe(object(), object())
        assert await probe.snapshot("desktop_click") is None
        assert await probe.snapshot("browser_click") is None
