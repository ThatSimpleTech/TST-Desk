"""Slack incoming webhook (TD-3801).

``send(config, message)`` posts only when Slack is enabled and the
injected (or keychain) URL's host matches ``notify.slack.host``. Off by
default. Failures never raise. The webhook URL never appears in logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from tstd.config import (
    ModelConfig,
    NotifyConfig,
    Preset,
    SlackNotifyConfig,
    TierConfig,
    cached_config,
    default_config_yaml,
    load_config,
)
from tstd.daemon import Daemon
from tstd.notify.slack import message_for, schedule, send
from tstd.protocol import ApprovalRequest, CostUpdate, TurnComplete
from tstd.session import SessionEventLog


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cached_config.cache_clear()


def _tier() -> TierConfig:
    return TierConfig(
        slug="t",
        base_url="http://127.0.0.1:11434/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=8192,
        max_output_tokens=256,
    )


def _config(*, enabled: bool = False, host: str = "") -> ModelConfig:
    preset = Preset(brain=_tier(), worker=_tier(), validator=_tier())
    return ModelConfig(
        presets={"t": preset},
        active_preset="t",
        notify=NotifyConfig(slack=SlackNotifyConfig(enabled=enabled, host=host, timeout_seconds=1)),
    )


def _approval() -> ApprovalRequest:
    return ApprovalRequest(
        session_id="s1",
        tool_call_id="tc-1",
        tool_name="shell",
        arguments={"command": "echo hi"},
        decision_class="B",
        summary="Run `echo hi`",
        reason="decision class B requires approval",
        seq=1,
    )


def _turn(*, failed: bool = False) -> TurnComplete:
    return TurnComplete(
        session_id="s1",
        tokens=1,
        cost=0,
        tier="worker",
        duration=0.1,
        failed=failed,
        error_code="auth_failed" if failed else None,
        seq=2,
    )


def _mock_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, request=request)

    return httpx.MockTransport(handler)


class TestSend:
    async def test_off_by_default_does_not_post(self) -> None:
        captured: list[httpx.Request] = []
        await send(
            _config(),
            "hello",
            webhook_url="http://127.0.0.1:9/hook",
            transport=_mock_transport(captured),
        )
        assert captured == []

    async def test_enabled_posts_injected_url(self) -> None:
        captured: list[httpx.Request] = []
        url = "http://127.0.0.1:9/services/T/B/injected"
        await send(
            _config(enabled=True, host="127.0.0.1"),
            "Approval needed: Run `echo hi`",
            webhook_url=url,
            transport=_mock_transport(captured),
        )
        assert len(captured) == 1
        assert str(captured[0].url) == url
        assert captured[0].method == "POST"
        assert json.loads(captured[0].read()) == {"text": "Approval needed: Run `echo hi`"}

    async def test_host_mismatch_does_not_post(self) -> None:
        captured: list[httpx.Request] = []
        await send(
            _config(enabled=True, host="allowed.example"),
            "hello",
            webhook_url="http://127.0.0.1:9/hook",
            transport=_mock_transport(captured),
        )
        assert captured == []

    async def test_empty_host_does_not_post(self) -> None:
        captured: list[httpx.Request] = []
        await send(
            _config(enabled=True, host=""),
            "hello",
            webhook_url="http://127.0.0.1:9/hook",
            transport=_mock_transport(captured),
        )
        assert captured == []

    async def test_http_error_is_logged_without_url_and_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        token = "T00/B00/webhook-secret-token"

        def boom(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, request=request)

        url = f"http://127.0.0.1:9/services/{token}"
        with caplog.at_level(logging.WARNING, logger="tstd.notify.slack"):
            await send(
                _config(enabled=True, host="127.0.0.1"),
                "hello",
                webhook_url=url,
                transport=httpx.MockTransport(boom),
            )
        assert any("slack notify failed" in r.message for r in caplog.records)
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert token not in joined
        assert url not in joined

    async def test_missing_keychain_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tstd.keychain import KeychainError

        async def missing() -> str:
            raise KeychainError("not stored")

        monkeypatch.setattr("tstd.notify.slack.get_slack_webhook_url", missing)
        await send(_config(enabled=True, host="127.0.0.1"), "hello")


class TestMessageAndSchedule:
    def test_message_for_approval_and_turn(self) -> None:
        assert message_for(_approval()) == "Approval needed: Run `echo hi`"
        assert message_for(_turn()) == "Turn complete: The agent finished a turn"
        assert message_for(_turn(failed=True)) == "Turn failed: auth_failed"
        assert (
            message_for(
                CostUpdate(session_id="s1", turn_cost=0, session_cost=0, total_cost=0, seq=1)
            )
            is None
        )

    async def test_schedule_delivers_approval_and_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        posted: list[str] = []

        async def fake_send(config: ModelConfig, message: str, **kwargs: object) -> None:
            posted.append(message)

        monkeypatch.setattr("tstd.notify.slack.send", fake_send)
        config = _config(enabled=True, host="127.0.0.1")
        approval_task = schedule(config, _approval(), webhook_url="http://127.0.0.1:9/hook")
        turn_task = schedule(config, _turn(), webhook_url="http://127.0.0.1:9/hook")
        assert approval_task is not None
        assert turn_task is not None
        await asyncio.gather(approval_task, turn_task)
        assert any(m.startswith("Approval needed") for m in posted)
        assert any(m.startswith("Turn complete") for m in posted)

    async def test_schedule_skips_when_disabled(self) -> None:
        assert schedule(_config(), _approval()) is None
        assert schedule(_config(), _turn()) is None


class TestDaemonHook:
    async def test_approval_and_turn_complete_deliver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[httpx.Request] = []
        token = "T00/B00/webhook-secret-token"
        url = f"http://127.0.0.1:9/services/{token}"
        transport = _mock_transport(captured)
        real_send = send

        async def wrapped(config: ModelConfig, message: str, **kwargs: object) -> None:
            await real_send(config, message, webhook_url=url, transport=transport)

        monkeypatch.setattr("tstd.notify.slack.send", wrapped)
        daemon = Daemon(data_dir=tmp_path / "data")
        daemon.config = _config(enabled=True, host="127.0.0.1")
        event_log = SessionEventLog()
        await daemon._on_session_event(_approval(), event_log)
        await daemon._on_session_event(_turn(), event_log)
        pending = [task for task in daemon._tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending)
        texts = [json.loads(req.read())["text"] for req in captured]
        assert any(text.startswith("Approval needed") for text in texts)
        assert any(text.startswith("Turn complete") for text in texts)
        blob = ""
        for path in (tmp_path / "data").rglob("*"):
            if path.is_file():
                blob += path.read_text(encoding="utf-8", errors="replace")
        assert token not in blob
        assert url not in blob

    async def test_other_events_do_not_deliver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        posted: list[str] = []

        async def fake_send(config: ModelConfig, message: str, **kwargs: object) -> None:
            posted.append(message)

        monkeypatch.setattr("tstd.notify.slack.send", fake_send)
        daemon = Daemon(data_dir=tmp_path / "data")
        daemon.config = _config(enabled=True, host="127.0.0.1")
        event = CostUpdate(session_id="s1", turn_cost=0, session_cost=0, total_cost=0, seq=1)
        await daemon._on_session_event(event, SessionEventLog())
        pending = [task for task in daemon._tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending)
        assert posted == []


class TestConfig:
    def test_shipped_slack_is_off(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(default_config_yaml(), encoding="utf-8")
        cfg = load_config(path)
        assert cfg.notify.slack.enabled is False
        assert cfg.notify.slack.host == ""

    def test_older_user_copy_without_notify_loads(self, tmp_path: Path) -> None:
        text = default_config_yaml()
        cut = text.index("\nnotify:")
        path = tmp_path / "config.yaml"
        path.write_text(text[:cut] + "\n", encoding="utf-8")
        cfg = load_config(path)
        assert cfg.notify.slack.enabled is False

    def test_schema_does_not_hold_a_webhook_url(self) -> None:
        import yaml

        assert "url" not in SlackNotifyConfig.model_fields
        assert "webhook_url" not in SlackNotifyConfig.model_fields
        shipped = yaml.safe_load(default_config_yaml())
        assert "url" not in shipped["notify"]["slack"]
        assert "webhook_url" not in shipped["notify"]["slack"]
