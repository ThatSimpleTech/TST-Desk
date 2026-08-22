"""UI-TARS click grounding (TD-3902).

The client is OpenAI-compatible vision chat. CI never talks to a live
model: a double or a fake transport supplies the point. Off, down, or
unresolved falls back to the intended (x, y) — TD-3304's path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.test_dispatch import attach_auto_approver
from tstd.autonomy import AmbiguousClassifier, Boundary, DecisionClassifier
from tstd.config import GroundingConfig
from tstd.desktop.grounding_client import (
    GroundingClient,
    GroundingResult,
    as_logical_points,
    parse_grounding_point,
)
from tstd.desktop.mock import MockDesktopDriver
from tstd.desktop.protocol import TINY_PNG
from tstd.policy import PolicyConfig
from tstd.tools import ToolDispatcher, create_registry
from tstd.tools.boundary import PathGuard
from tstd.tools.desktop import aim_desktop_click
from tstd.tools.handlers import register_builtin_handlers


class _HitClient:
    enabled = True

    def __init__(self, x: float = 80.0, y: float = 90.0, latency_ms: float = 4.5) -> None:
        self.x = x
        self.y = y
        self.latency_ms = latency_ms
        self.seen: list[tuple[bytes, str]] = []

    async def locate(
        self,
        screenshot_png: bytes,
        target: str,
        *,
        width_points: float | None = None,
        height_points: float | None = None,
    ) -> GroundingResult:
        self.seen.append((screenshot_png, target))
        return GroundingResult.hit(self.x, self.y, latency_ms=self.latency_ms)


class _MissClient:
    enabled = True

    async def locate(
        self,
        screenshot_png: bytes,
        target: str,
        *,
        width_points: float | None = None,
        height_points: float | None = None,
    ) -> GroundingResult:
        return GroundingResult.fallback(reason="unparseable", latency_ms=2.0)


def _dispatcher(
    workspace: Path,
    driver: MockDesktopDriver,
    client: _HitClient | _MissClient | GroundingClient | None,
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
    register_builtin_handlers(dispatcher, desktop_driver=driver, grounding_client=client)
    attach_auto_approver(dispatcher)
    return dispatcher


class TestParse:
    def test_json_is_logical_points(self) -> None:
        parsed = parse_grounding_point('here: {"x": 120.5, "y": 80}')
        assert parsed is not None
        assert parsed.space == "points"
        assert as_logical_points(parsed, width_points=800, height_points=400) == (120.5, 80.0)

    def test_uitars_start_box_is_norm1000(self) -> None:
        parsed = parse_grounding_point("Action: click(start_box='(500, 250)')")
        assert parsed is not None
        assert parsed.space == "norm1000"
        assert as_logical_points(parsed, width_points=800, height_points=400) == (400.0, 100.0)

    def test_norm1000_without_size_is_unusable(self) -> None:
        parsed = parse_grounding_point("click(start_box='(10, 20)')")
        assert parsed is not None
        assert as_logical_points(parsed, width_points=None, height_points=None) is None

    def test_unreadable_text_is_none(self) -> None:
        assert parse_grounding_point("I cannot see that") is None


class TestClient:
    def test_empty_base_url_disables(self) -> None:
        client = GroundingClient.from_config(GroundingConfig())
        assert client.enabled is False

    async def test_disabled_does_not_open_a_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("disabled client must not open a transport")

        monkeypatch.setattr(httpx.AsyncClient, "post", _forbidden)
        result = await GroundingClient("", None, 1).locate(TINY_PNG, "Save")
        assert result.source == "intended"
        assert result.reason == "off"
        assert result.cost == 0.0
        assert result.latency_ms == 0.0

    async def test_down_loopback_falls_back(self) -> None:
        client = GroundingClient("http://127.0.0.1:1/v1", "any", 0.2)
        result = await client.locate(TINY_PNG, "Save")
        assert result.source == "intended"
        assert result.reason == "down"
        assert result.cost == 0.0
        assert result.latency_ms >= 0.0

    async def test_missing_model_falls_back(self) -> None:
        client = GroundingClient("http://127.0.0.1:1/v1", None, 0.2)
        result = await client.locate(TINY_PNG, "Save")
        assert result.source == "intended"
        assert result.reason == "unresolved"
        assert result.cost == 0.0

    async def test_fake_completion_returns_a_point(self) -> None:
        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": '{"x": 64.0, "y": 32.0}'}}]}

        class _Http:
            def __init__(self) -> None:
                self.urls: list[str] = []

            async def post(self, url: str, json: dict[str, Any]) -> _Response:
                self.urls.append(url)
                assert json["model"] == "test-grounder"
                return _Response()

        http = _Http()
        client = GroundingClient(
            "http://127.0.0.1:8000/v1",
            "test-grounder",
            1.0,
            client=http,  # type: ignore[arg-type]
        )
        result = await client.locate(TINY_PNG, "OK")
        assert result.source == "model"
        assert (result.x, result.y) == (64.0, 32.0)
        assert result.cost == 0.0
        assert result.latency_ms >= 0.0
        assert http.urls == ["http://127.0.0.1:8000/v1/chat/completions"]

    async def test_unparseable_reply_falls_back(self) -> None:
        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "nope"}}]}

        class _Http:
            async def post(self, url: str, json: dict[str, Any]) -> _Response:
                return _Response()

        client = GroundingClient(
            "http://127.0.0.1:8000/v1",
            "test-grounder",
            1.0,
            client=_Http(),  # type: ignore[arg-type]
        )
        result = await client.locate(TINY_PNG, "OK")
        assert result.source == "intended"
        assert result.reason == "unparseable"
        assert result.cost == 0.0


class TestGlue:
    async def test_hit_replaces_the_intended_point(self) -> None:
        driver = MockDesktopDriver()
        client = _HitClient(55.0, 66.0, latency_ms=3.0)
        x, y, result = await aim_desktop_click(driver, 10.0, 20.0, target="Save", client=client)
        assert (x, y) == (55.0, 66.0)
        assert result.source == "model"
        assert result.cost == 0.0
        assert result.latency_ms == 3.0
        assert client.seen[0][0].startswith(b"\x89PNG")
        assert "Save" in client.seen[0][1]

    async def test_off_keeps_the_intended_point(self) -> None:
        driver = MockDesktopDriver()
        x, y, result = await aim_desktop_click(driver, 10.0, 20.0, target="Save", client=None)
        assert (x, y) == (10.0, 20.0)
        assert result.source == "intended"
        assert result.reason == "off"
        assert result.cost == 0.0
        assert driver.calls == []

    async def test_down_url_keeps_the_intended_point(self) -> None:
        driver = MockDesktopDriver()
        client = GroundingClient("http://127.0.0.1:1/v1", "any", 0.2)
        x, y, result = await aim_desktop_click(driver, 10.0, 20.0, target="Save", client=client)
        assert (x, y) == (10.0, 20.0)
        assert result.source == "intended"
        assert result.cost == 0.0
        assert result.latency_ms >= 0.0
        assert [name for name, _ in driver.calls] == ["screenshot"]

    async def test_miss_keeps_the_intended_point(self) -> None:
        driver = MockDesktopDriver()
        x, y, result = await aim_desktop_click(
            driver, 10.0, 20.0, target="Save", client=_MissClient()
        )
        assert (x, y) == (10.0, 20.0)
        assert result.source == "intended"
        assert result.reason == "unparseable"


class TestClickPath:
    async def test_mock_point_is_what_the_click_uses(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver()
        client = _HitClient(80.0, 90.0, latency_ms=4.5)
        dispatcher = _dispatcher(tmp_path, driver, client)
        result = await dispatcher.dispatch(
            "c1",
            "desktop_click",
            {"x": 10, "y": 20, "target": "Save"},
        )
        assert result.status == "success"
        body = json.loads(result.output)
        assert body["clicked"] == {"x": 80.0, "y": 90.0}
        assert body["grounding"]["source"] == "model"
        assert body["grounding"]["cost"] == 0.0
        assert body["grounding"]["latency_ms"] == 4.5
        click = [kwargs for name, kwargs in driver.calls if name == "click"]
        assert click == [
            {"x": 80.0, "y": 90.0, "button": "left", "count": 1, "expect_window": None}
        ]

    async def test_empty_config_uses_td3304_point(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver()
        dispatcher = _dispatcher(tmp_path, driver, None)
        result = await dispatcher.dispatch("c1", "desktop_click", {"x": 10, "y": 20})
        assert result.status == "success"
        body = json.loads(result.output)
        assert body["clicked"] == {"x": 10, "y": 20}
        assert body["grounding"]["source"] == "intended"
        assert body["grounding"]["cost"] == 0.0
        assert body["grounding"]["latency_ms"] == 0.0
        click = [kwargs for name, kwargs in driver.calls if name == "click"]
        assert click[0]["x"] == 10
        assert click[0]["y"] == 20

    async def test_down_url_uses_td3304_point(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver()
        client = GroundingClient("http://127.0.0.1:1/v1", "any", 0.2)
        dispatcher = _dispatcher(tmp_path, driver, client)
        result = await dispatcher.dispatch("c1", "desktop_click", {"x": 10, "y": 20})
        assert result.status == "success"
        body = json.loads(result.output)
        assert body["clicked"] == {"x": 10, "y": 20}
        assert body["grounding"]["source"] == "intended"
        assert body["grounding"]["cost"] == 0.0
        assert body["grounding"]["latency_ms"] >= 0.0
