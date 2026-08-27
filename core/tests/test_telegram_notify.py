"""Telegram Bot API sendMessage (TD-4707).

``send(config, message)`` posts only when Telegram is enabled, the
injected (or keychain) URL's host matches ``notify.telegram.host``, and
the URL carries ``chat_id`` as a query parameter. Off by default.
Failures never raise. The bot token never appears in logs.
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
    TelegramNotifyConfig,
    TierConfig,
    cached_config,
    default_config_yaml,
    load_config,
)
from tstd.daemon import Daemon
from tstd.notify.telegram import message_for, schedule, send
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
        notify=NotifyConfig(
            telegram=TelegramNotifyConfig(enabled=enabled, host=host, timeout_seconds=1)
        ),
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
            bot_url="http://127.0.0.1:9/botSECRET/sendMessage?chat_id=1",
            transport=_mock_transport(captured),
        )
        assert captured == []

    async def test_enabled_posts_chat_id_and_text(self) -> None:
        captured: list[httpx.Request] = []
        token = "botSECRETTOKEN"
        url = f"http://127.0.0.1:9/{token}/sendMessage?chat_id=-1001"
        await send(
            _config(enabled=True, host="127.0.0.1"),
            "Turn complete: The agent finished a turn",
            bot_url=url,
            transport=_mock_transport(captured),
        )
        assert len(captured) == 1
        assert str(captured[0].url) == "http://127.0.0.1:9/botSECRETTOKEN/sendMessage"
        assert json.loads(captured[0].read()) == {
            "chat_id": "-1001",
            "text": "Turn complete: The agent finished a turn",
        }

    async def test_missing_chat_id_does_not_post(self) -> None:
        captured: list[httpx.Request] = []
        await send(
            _config(enabled=True, host="127.0.0.1"),
            "hello",
            bot_url="http://127.0.0.1:9/botSECRET/sendMessage",
            transport=_mock_transport(captured),
        )
        assert captured == []

    async def test_host_mismatch_does_not_post(self) -> None:
        captured: list[httpx.Request] = []
        await send(
            _config(enabled=True, host="allowed.example"),
            "hello",
            bot_url="http://127.0.0.1:9/botSECRET/sendMessage?chat_id=1",
            transport=_mock_transport(captured),
        )
        assert captured == []

    async def test_http_error_is_logged_without_url_and_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        token = "botSECRETTOKEN"

        def boom(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, request=request)

        url = f"http://127.0.0.1:9/{token}/sendMessage?chat_id=1"
        with caplog.at_level(logging.WARNING, logger="tstd.notify.telegram"):
            await send(
                _config(enabled=True, host="127.0.0.1"),
                "hello",
                bot_url=url,
                transport=httpx.MockTransport(boom),
            )
        assert any("telegram notify failed" in r.message for r in caplog.records)
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert token not in joined
        assert url not in joined

    async def test_missing_keychain_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tstd.keychain import KeychainError

        async def missing() -> str:
            raise KeychainError("not stored")

        monkeypatch.setattr("tstd.notify.telegram.get_telegram_bot_url", missing)
        await send(_config(enabled=True, host="127.0.0.1"), "hello")


class TestMessageAndSchedule:
    def test_message_for_approval_and_turn(self) -> None:
        assert message_for(_approval()) == "Approval needed: Run `echo hi`"
        assert message_for(_turn()) == "Turn complete: The agent finished a turn"
        assert (
            message_for(
                CostUpdate(session_id="s1", turn_cost=0, session_cost=0, total_cost=0, seq=1)
            )
            is None
        )

    async def test_schedule_delivers_approval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        posted: list[str] = []

        async def fake_send(config: ModelConfig, message: str, **kwargs: object) -> None:
            posted.append(message)

        monkeypatch.setattr("tstd.notify.telegram.send", fake_send)
        task = schedule(
            _config(enabled=True, host="127.0.0.1"),
            _approval(),
            bot_url="http://127.0.0.1:9/botX/sendMessage?chat_id=1",
        )
        assert task is not None
        await asyncio.gather(task)
        assert any(m.startswith("Approval needed") for m in posted)

    async def test_schedule_skips_when_disabled(self) -> None:
        assert schedule(_config(), _approval()) is None


class TestDaemonHook:
    async def test_approval_delivers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        posted: list[str] = []

        async def fake_send(config: ModelConfig, message: str, **kwargs: object) -> None:
            posted.append(message)

        monkeypatch.setattr("tstd.notify.telegram.send", fake_send)
        daemon = Daemon(data_dir=tmp_path / "data")
        daemon.config = _config(enabled=True, host="127.0.0.1")
        await daemon._on_session_event(_approval(), SessionEventLog())
        pending = [task for task in daemon._tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending)
        assert any(m.startswith("Approval needed") for m in posted)


class TestConfig:
    def test_shipped_telegram_is_off(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(default_config_yaml(), encoding="utf-8")
        cfg = load_config(path)
        assert cfg.notify.telegram.enabled is False
        assert cfg.notify.telegram.host == ""

    def test_schema_does_not_hold_a_bot_url(self) -> None:
        import yaml

        assert "bot_url" not in TelegramNotifyConfig.model_fields
        assert "chat_id" not in TelegramNotifyConfig.model_fields
        shipped = yaml.safe_load(default_config_yaml())
        assert "bot_url" not in shipped["notify"]["telegram"]
        assert "chat_id" not in shipped["notify"]["telegram"]
