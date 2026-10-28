"""Backup endpoints per tier (TD-5120).

A tier lists ``fallbacks``. When its own endpoint is down, the provider
spends ``provider_retry`` on it as before, and then the turn sends the same
request to the next backup instead of failing. Each endpoint below is a
host on one mock transport, reached through the real ``ProviderClient``,
so the real retry policy, error mapping and stream watchdog run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.test_dispatch import (
    attach_auto_approver,
    make_registry_and_dispatcher,
    wait_for_turn,
)
from tests.test_turn_stream_errors import (
    _DONE,
    _SSE,
    _drops_after,
    _event,
    _litellm_error,
    _openrouter_error,
    _text,
)
from tstd.config import (
    CredentialConfig,
    ModelConfig,
    Preset,
    ProviderRetryConfig,
    TierConfig,
)
from tstd.context.calibration import CalibrationStore
from tstd.keychain import KeychainError
from tstd.loop import agent_loop
from tstd.protocol import (
    AssistantDelta,
    AssistantRetract,
    ContextUsage,
    ModelFallback,
    TierState,
    TurnComplete,
)
from tstd.provider import ProviderClient, RetryConfig
from tstd.reply_retry import shown_deltas
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import ToolDispatcher, ToolRegistry

PRIMARY = "http://primary.local/v1"
BACKUP = "http://backup.local/v1"
SECOND = "http://second.local/v1"

Script = Callable[[int], "httpx.Response | Exception | bytes | AsyncIterator[bytes]"]


def _tier(
    slug: str,
    base_url: str,
    *,
    window: int = 100_000,
    price: float = 0.0,
    fallbacks: list[TierConfig] | None = None,
) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url=base_url,
        input_price=price,
        output_price=price * 5,
        cache_read_price=price / 10,
        context_window=window,
        max_output_tokens=1_000,
        # The idle watchdog scaled down, so a stall trips in 0.2 s.
        stream_idle_timeout=0.2,
        fallbacks=fallbacks or [],
    )


def _config(*backups: TierConfig, retries: int = 2) -> ModelConfig:
    brain = _tier("own-model", PRIMARY, fallbacks=list(backups))
    config = ModelConfig(
        presets={
            "test": Preset(
                brain=brain,
                worker=_tier("test-worker", "http://worker.local/v1"),
                validator=_tier("test-validator", "http://validator.local/v1"),
            )
        },
        active_preset="test",
        credentials={"spare": CredentialConfig(name="Spare")},
    )
    config.provider_retry = ProviderRetryConfig(
        max_retries=retries, initial_delay=0.001, max_delay=0.001
    )
    return config


BACKUP_TIER = _tier("backup-model", BACKUP, window=32_768, price=3.0)
SECOND_TIER = _tier("second-model", SECOND, window=65_536, price=1.0)


def _answer(text: str, prompt_tokens: int = 3_000) -> bytes:
    """A finished reply that reports usage, so cost and calibration see it."""
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": 10, "total_tokens": 3_010}
    finish = {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return _text(text) + _event({**finish, "usage": usage}) + _DONE


def _tool_call(message: str) -> bytes:
    call = {
        "index": 0,
        "id": "call-1",
        "type": "function",
        "function": {"name": "echo", "arguments": json.dumps({"message": message})},
    }
    return (
        _event({"id": "c1", "choices": [{"index": 0, "delta": {"tool_calls": [call]}}]})
        + _text("", finish="tool_calls")
        + _DONE
    )


async def _stalls_after(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk
    await asyncio.sleep(5)


def _refused(_: int) -> Exception:
    return httpx.ConnectError("[Errno 111] Connection refused")


def _status(code: int) -> Script:
    return lambda _: httpx.Response(code, json={"error": {"message": f"upstream said {code}"}})


class _Endpoints:
    """One mock transport for every host; each host follows its own script."""

    def __init__(self, scripts: dict[str, Script]) -> None:
        self._scripts = scripts
        self.hits: list[tuple[str, str]] = []

    def count(self, base_url: str) -> int:
        host = httpx.URL(base_url).host
        return sum(1 for hit, _ in self.hits if hit == host)

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        model = json.loads(request.content)["model"]
        self.hits.append((host, model))
        attempt = self.count(f"http://{host}")
        # A tier bound to a named key sends it; a script for that key on
        # this host (`<base_url>#<key>`) answers for it.
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        base = f"http://{host}/v1"
        reply = self._scripts.get(f"{base}#{key}", self._scripts[base])(attempt)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, headers=_SSE, content=reply)

    def client(self, tier: TierConfig, retries: int) -> ProviderClient:
        return ProviderClient(
            tier.base_url,
            tier.credential,
            timeout=tier.timeouts(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            retry_config=RetryConfig(
                max_retries=retries, initial_delay=0.001, max_delay=0.001, jitter_factor=0.0
            ),
        )


class _Run:
    """A session on a brain-only router, driven one message at a time."""

    def __init__(
        self,
        tmp_path: Path,
        endpoints: _Endpoints,
        config: ModelConfig,
        *,
        registry: ToolRegistry | None = None,
        dispatcher: ToolDispatcher | None = None,
        missing_key: set[str] | None = None,
    ) -> None:
        self.session = Session(str(tmp_path))
        self.calibration = CalibrationStore()
        retries = config.provider_retry.max_retries
        missing = missing_key or set()

        async def factory(tier: TierConfig) -> ProviderClient:
            if tier.base_url in missing:
                raise KeychainError(f"API key not found in keychain for {tier.base_url!r}.")
            return endpoints.client(tier, retries)

        self.runner = SessionRunner(
            self.session,
            loop_factory=lambda s: agent_loop(
                s,
                TierRouter(lead_turns=50),
                factory,
                config,
                tool_registry=registry,
                tool_dispatcher=dispatcher or attach_auto_approver(ToolDispatcher(ToolRegistry())),
                calibration_store=self.calibration,
            ),
        )
        self.turns = 0

    async def send(self, text: str) -> TurnComplete:
        if self.turns == 0:
            await self.runner.start()
        self.turns += 1
        await self.session.add_user_message(text)
        return await wait_for_turn(self.session, self.turns, 5.0)

    async def close(self) -> None:
        await self.runner.cancel()

    def of(self, kind: type[Any]) -> list[Any]:
        return [e for e in self.session.event_log.all_events if isinstance(e, kind)]

    def shown(self) -> str:
        return "".join(shown_deltas(self.session.event_log.all_events))


async def _one_turn(
    tmp_path: Path, scripts: dict[str, Script], config: ModelConfig, **kwargs: Any
) -> tuple[_Run, _Endpoints, TurnComplete]:
    endpoints = _Endpoints(scripts)
    run = _Run(tmp_path, endpoints, config, **kwargs)
    try:
        turn = await run.send("fix it")
    finally:
        await run.close()
    return run, endpoints, turn


# ── what moves a turn to the backup ──────────────────────────────────────


class TestTheBackupAnswers:
    async def test_a_refused_connection_moves_after_the_retry_budget(self, tmp_path: Path) -> None:
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {PRIMARY: _refused, BACKUP: lambda _: _answer("From the backup.")},
            _config(BACKUP_TIER, retries=2),
        )

        # Before TD-5120 the turn failed here as connection_error.
        assert not turn.failed
        # The whole retry budget went to the tier's own endpoint first.
        assert endpoints.count(PRIMARY) == 3
        assert endpoints.hits[-1] == ("backup.local", "backup-model")
        assert run.shown() == "From the backup."
        assert [str(m.content) for m in run.session.conversation if m.role == "assistant"] == [
            "From the backup."
        ]
        (moved,) = run.of(ModelFallback)
        assert (moved.tier, moved.reason, moved.fallback) == ("brain", "connection_error", 1)
        assert (moved.from_model, moved.from_host) == ("own-model", "primary.local")
        assert (moved.to_model, moved.to_host) == ("backup-model", "backup.local")
        # The timeline row comes before anything the backup says.
        events = run.session.event_log.all_events
        first_delta = next(i for i, e in enumerate(events) if isinstance(e, AssistantDelta))
        assert events.index(moved) < first_delta

    @pytest.mark.parametrize(
        ("script", "reason"),
        [
            (_status(503), "service_unavailable"),
            (_status(500), "server_error"),
            (_status(502), "bad_gateway"),
            (_status(429), "rate_limited"),
            # The idle watchdog: the server takes the request and goes quiet.
            (lambda _: _stalls_after(), "stream_stalled"),
            # A 5xx reported in-band before any output (TD-5152).
            (lambda _: _litellm_error("upstream failed", "503"), "service_unavailable"),
            # The server restarts while it reads the prompt: the headers
            # arrived, then the connection dropped before any output.
            (lambda _: _drops_after(), "request_error"),
        ],
        ids=["503", "500", "502", "429", "idle-watchdog", "in-band-5xx", "dropped"],
    )
    async def test_5xx_rate_limits_and_stalls_move_too(
        self, tmp_path: Path, script: Script, reason: str
    ) -> None:
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {PRIMARY: script, BACKUP: lambda _: _answer("From the backup.")},
            _config(BACKUP_TIER, retries=2),
        )

        assert not turn.failed
        assert endpoints.count(PRIMARY) == 3
        assert endpoints.count(BACKUP) == 1
        assert run.shown() == "From the backup."
        assert [m.reason for m in run.of(ModelFallback)] == [reason]

    async def test_no_retry_budget_moves_on_the_first_failure(self, tmp_path: Path) -> None:
        _, endpoints, turn = await _one_turn(
            tmp_path,
            {PRIMARY: _refused, BACKUP: lambda _: _answer("ok")},
            _config(BACKUP_TIER, retries=0),
        )

        assert not turn.failed
        assert (endpoints.count(PRIMARY), endpoints.count(BACKUP)) == (1, 1)

    async def test_a_stall_partway_is_withdrawn_before_the_backup_answers(
        self, tmp_path: Path
    ) -> None:
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {
                PRIMARY: lambda n: _stalls_after(_text(f"Half an answer {n}")),
                BACKUP: lambda _: _answer("The whole answer."),
            },
            _config(BACKUP_TIER, retries=2),
        )

        assert not turn.failed
        # TD-5159 asks the tier's own model once more first; that is the
        # budget for a stall partway through a reply.
        assert endpoints.count(PRIMARY) == 2
        retracts = run.of(AssistantRetract)
        assert len(retracts) == 2
        # Both partial replies came off the window; only the backup's stays.
        assert run.shown() == "The whole answer."
        (moved,) = run.of(ModelFallback)
        assert moved.reason == "stream_stalled"
        events = run.session.event_log.all_events
        assert events.index(retracts[-1]) < events.index(moved)

    @pytest.mark.parametrize(
        "script",
        [
            lambda _: _drops_after(_text("Half an answer ")),
            lambda _: _text("Half an answer ") + _litellm_error("upstream failed", "500"),
            lambda _: _text("Half an answer ") + _litellm_error("busy", "429"),
            lambda _: _text("Half an answer ") + _openrouter_error("Provider disconnected"),
        ],
        ids=["dropped", "litellm-500", "litellm-429", "openrouter-error"],
    )
    async def test_a_reply_the_endpoint_cuts_off_is_withdrawn_before_the_backup_answers(
        self, tmp_path: Path, script: Script
    ) -> None:
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {PRIMARY: script, BACKUP: lambda _: _answer("The whole answer.")},
            _config(BACKUP_TIER, retries=2),
        )

        # Before the review fix the turn failed here as stream_interrupted.
        assert not turn.failed
        # The tier's own endpoint is asked again twice first (TD-5183), as a
        # stall is once (TD-5159); then the backup answers.
        assert (endpoints.count(PRIMARY), endpoints.count(BACKUP)) == (3, 1)
        retracts = run.of(AssistantRetract)
        assert len(retracts) == 3
        (moved,) = run.of(ModelFallback)
        assert moved.reason == "stream_interrupted"
        events = run.session.event_log.all_events
        assert events.index(retracts[-1]) < events.index(moved)
        assert run.shown() == "The whole answer."

    async def test_a_second_key_on_the_same_host_gets_its_own_client(self, tmp_path: Path) -> None:
        # The same model and host with another key: a rate-limited key's
        # backup. The loop's client cache used to key on the host alone, so
        # the backup reused the tier's client and sent the tier's key.
        spare = _tier("own-model", PRIMARY).model_copy(update={"credential": "spare"})
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {PRIMARY: _status(429), f"{PRIMARY}#spare": lambda _: _answer("On the spare key.")},
            _config(spare, retries=1),
        )

        assert not turn.failed
        assert run.shown() == "On the spare key."
        assert endpoints.count(PRIMARY) == 3
        (moved,) = run.of(ModelFallback)
        assert (moved.reason, moved.to_model, moved.to_host) == (
            "rate_limited",
            "own-model",
            "primary.local",
        )

    async def test_backups_are_tried_in_order(self, tmp_path: Path) -> None:
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {
                PRIMARY: _refused,
                BACKUP: _status(503),
                SECOND: lambda _: _answer("From the second backup."),
            },
            _config(BACKUP_TIER, SECOND_TIER, retries=1),
        )

        assert not turn.failed
        assert [host for host, _ in endpoints.hits] == [
            "primary.local",
            "primary.local",
            "backup.local",
            "backup.local",
            "second.local",
        ]
        assert [(m.fallback, m.from_model, m.to_model) for m in run.of(ModelFallback)] == [
            (1, "own-model", "backup-model"),
            (2, "backup-model", "second-model"),
        ]
        assert run.shown() == "From the second backup."

    async def test_the_last_backup_failing_ends_the_turn_with_its_error(
        self, tmp_path: Path
    ) -> None:
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {PRIMARY: _refused, BACKUP: _status(503)},
            _config(BACKUP_TIER, retries=1),
        )

        assert (turn.failed, turn.error_code) == (True, "service_unavailable")
        assert (endpoints.count(PRIMARY), endpoints.count(BACKUP)) == (2, 2)
        assert len(run.of(ModelFallback)) == 1

    async def test_a_backup_with_no_stored_key_fails_the_turn_saying_so(
        self, tmp_path: Path
    ) -> None:
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {PRIMARY: _refused, BACKUP: lambda _: _answer("unreachable")},
            _config(BACKUP_TIER, retries=0),
            missing_key={BACKUP},
        )

        assert (turn.failed, turn.error_code) == (True, "missing_api_key")
        assert endpoints.count(BACKUP) == 0
        assert "API key not found" in run.shown()


class TestWhatDoesNotMove:
    @pytest.mark.parametrize(
        ("script", "code"),
        [
            (_status(401), "auth_failed"),
            (_status(400), "bad_request"),
            (_status(404), "not_found"),
        ],
        ids=["401", "400", "404"],
    )
    async def test_a_failure_of_the_request_stays_on_the_tier(
        self, tmp_path: Path, script: Script, code: str
    ) -> None:
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {PRIMARY: script, BACKUP: lambda _: _answer("never")},
            _config(BACKUP_TIER, retries=2),
        )

        assert (turn.failed, turn.error_code) == (True, code)
        assert endpoints.count(BACKUP) == 0
        assert run.of(ModelFallback) == []

    @pytest.mark.parametrize(
        "script",
        [
            # The body just ends: no finish reason, no [DONE], no error.
            lambda _: _text("The answer is "),
            # The request's own failure, reported partway.
            lambda _: _text("The answer is ") + _litellm_error("bad request", "400"),
        ],
        ids=["just-ends", "in-band-400"],
    )
    async def test_a_cut_off_reply_that_names_no_endpoint_failure_stays(
        self, tmp_path: Path, script: Script
    ) -> None:
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {PRIMARY: script, BACKUP: lambda _: _answer("never")},
            _config(BACKUP_TIER, retries=2),
        )

        assert (turn.failed, turn.error_code) == (True, "stream_interrupted")
        assert endpoints.count(BACKUP) == 0
        assert run.of(ModelFallback) == []
        # TD-5152 as before: the partial text stays above the failure line.
        assert run.shown().startswith("The answer is ")

    async def test_a_tier_with_no_backups_fails_as_before(self, tmp_path: Path) -> None:
        run, endpoints, turn = await _one_turn(tmp_path, {PRIMARY: _refused}, _config(retries=2))

        assert (turn.failed, turn.error_code) == (True, "connection_error")
        assert endpoints.count(PRIMARY) == 3
        assert run.of(ModelFallback) == []
        assert all(not e.fallbacks for e in run.of(TierState))


# ── the rest of the turn, and the next one ───────────────────────────────


class TestStickiness:
    async def test_the_turn_stays_on_the_backup_and_the_next_tries_its_own_model(
        self, tmp_path: Path
    ) -> None:
        registry, dispatcher = make_registry_and_dispatcher()
        replies = iter([_tool_call("hi"), _answer("Done on the backup."), _answer("Turn two.")])
        endpoints = _Endpoints({PRIMARY: _refused, BACKUP: lambda _: next(replies)})
        run = _Run(
            tmp_path,
            endpoints,
            _config(BACKUP_TIER, retries=2),
            registry=registry,
            dispatcher=dispatcher,
        )
        try:
            first = await run.send("fix it")
            assert not first.failed
            # The model call after the tool went straight to the backup:
            # the dead endpoint cost its retry budget once this turn.
            assert (endpoints.count(PRIMARY), endpoints.count(BACKUP)) == (3, 2)
            states = run.of(TierState)
            assert states[-1].fallbacks == {"brain": 1}
            assert states[-1].model_slugs["brain"] == "backup-model"
            assert states[-1].hosts["brain"] == "backup.local"

            second = await run.send("and again")
            assert not second.failed
            # A new turn starts on the tier's own model again.
            assert (endpoints.count(PRIMARY), endpoints.count(BACKUP)) == (6, 3)
        finally:
            await run.close()

        assert len(run.of(ModelFallback)) == 2
        # Between the two switches the title bar was told the brain is on
        # its own model again.
        backups = [e.fallbacks for e in run.of(TierState)]
        assert {"brain": 1} in backups
        assert backups.index({"brain": 1}) < len(backups) - 1
        assert {} in backups[backups.index({"brain": 1}) :]


# ── the meter, cost and calibration follow the model in use ──────────────


class TestTheModelInUse:
    async def test_window_meter_price_and_calibration_are_the_backups(self, tmp_path: Path) -> None:
        run, _, turn = await _one_turn(
            tmp_path,
            {PRIMARY: _refused, BACKUP: lambda _: _answer("From the backup.", 4_000)},
            _config(BACKUP_TIER, retries=0),
        )

        assert not turn.failed
        # The gauge: the last reading is measured against the backup's window.
        usage = run.of(ContextUsage)
        assert usage[0].model == "own-model"
        assert (usage[0].context_window, usage[0].fallback) == (100_000, 0)
        assert usage[-1].model == "backup-model"
        assert (usage[-1].context_window, usage[-1].fallback) == (32_768, 1)
        # The meter: priced at the backup's rates, recorded under its slug.
        tracker = run.session.cost_tracker
        assert tracker is not None
        (call,) = tracker.calls
        assert call.model == "backup-model"
        assert call.cost == pytest.approx((4_000 * 3.0 + 10 * 15.0) / 1_000_000)
        # Calibration: the backup's usage teaches the backup's ratio only.
        assert run.calibration.ratio((BACKUP, "backup-model")) is not None
        assert run.calibration.ratio((PRIMARY, "own-model")) is None

    async def test_a_smaller_backup_window_is_fitted_before_the_backup_is_asked(
        self, tmp_path: Path
    ) -> None:
        tiny = _tier("tiny-model", BACKUP, window=4_096)
        run, endpoints, turn = await _one_turn(
            tmp_path,
            {PRIMARY: _refused, BACKUP: lambda _: _answer("ok", 1_000)},
            _config(tiny, retries=0),
        )

        assert not turn.failed
        last = run.of(ContextUsage)[-1]
        assert (last.context_window, last.model) == (4_096, "tiny-model")
        assert last.compact_at == int((4_096 - 1_000) * 0.8)
        assert endpoints.count(BACKUP) == 1
