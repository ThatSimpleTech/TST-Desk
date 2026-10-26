"""TD-5175: the reply guard cuts a reply that turned to junk and asks again.

Unit tests drive :class:`ReplyGuard` directly. Loop tests stream through the
real ``ProviderClient`` from a mock gateway (as test_stall_retry does): the
first attempt turns to junk, the retry answers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from tests.test_dispatch import attach_auto_approver, make_config, wait_for_turn
from tests.test_turn_stream_errors import _SSE, _answer, _event, _Gateway, _text
from tstd.config import ModelConfig, ProviderRetryConfig, ReplyGuardConfig
from tstd.loop import agent_loop
from tstd.protocol import AssistantRetract, TurnComplete
from tstd.provider import ProviderClient
from tstd.reply_guard import ReplyGuard, user_blocks
from tstd.reply_retry import GUARDED_CODE, shown_deltas
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import ToolDispatcher, ToolRegistry

SALAD = "ٍٍ จ<br>ٍٍ führٍٍٍ amb</ ٍٍٍTwoSam ٍٍٍZeroɏٍٍٍ ाि ٍٍٍٍ т ынٍ ٍтовٍٍٍ "
CODE = (
    "```ts\nexport function answer(item: Item, by: 'alex' | 'sam') {\n"
    "  return { ...item, status: 'answered', answeredBy: by, at: Date.now() }\n}\n```\n"
    "| Campo | Descripción |\n|---|---|\n| estado | pendiente / respondido |\n"
    "Listo: la bandeja muestra cada mensaje — señal → acción ✓ 你好\n"
)


def _varied(text: str, n: int) -> str:
    """*text* n times, each copy a little different, so it is not a loop."""
    return "".join(f"{i}. {text[i % 7 :]}{text[: i % 7]}\n" for i in range(n))


def _salad(n: int) -> str:
    return "".join(
        SALAD[(i * 5) % len(SALAD) :] + SALAD[: (i * 5) % len(SALAD)] + f" {i} " for i in range(n)
    )


def _feed(guard: ReplyGuard, text: str, *, reasoning: bool = False) -> str | None:
    for i in range(0, len(text), 37):
        part = text[i : i + 37]
        tripped = guard.feed(reasoning=part) if reasoning else guard.feed(content=part)
        if tripped:
            return tripped
    return None


class TestReplyGuard:
    def test_ordinary_code_markdown_and_spanish_pass(self) -> None:
        guard = ReplyGuard(ReplyGuardConfig())
        assert _feed(guard, CODE * 20) is None

    def test_script_salad_trips_junk(self) -> None:
        guard = ReplyGuard(ReplyGuardConfig())
        assert _feed(guard, _varied("Planning the store.", 10) + _salad(30)) == "junk"

    def test_a_script_the_user_writes_is_not_junk(self) -> None:
        russian = _varied("Привет, как дела? Сделай список дел на завтра.", 30)
        guard = ReplyGuard(ReplyGuardConfig(), allowed_blocks=user_blocks(["Привет, сделай"]))
        assert _feed(guard, russian) is None
        assert _feed(ReplyGuard(ReplyGuardConfig()), russian) == "junk"

    def test_a_short_unit_repeated_trips_repetition(self) -> None:
        guard = ReplyGuard(ReplyGuardConfig())
        assert _feed(guard, "Writing App.tsx.\n" + "448" * 80) == "repetition"

    def test_a_long_unit_repeated_trips_repetition(self) -> None:
        guard = ReplyGuard(ReplyGuardConfig(leaked_markup=False))
        assert _feed(guard, "ok\n" + "</\uff5cDSML\uff5c calls>\n" * 20) == "repetition"

    def test_separator_lines_are_not_repetition(self) -> None:
        guard = ReplyGuard(ReplyGuardConfig())
        assert _feed(guard, "Title\n" + "-" * 400 + "\n" + "=" * 400 + "\n") is None

    def test_tool_markup_in_text_trips_only_when_tools_were_offered(self) -> None:
        leak = 'Let me read it. <\uff5cDSML\uff5c invoke name="fs_read">'
        assert _feed(ReplyGuard(ReplyGuardConfig(), tools=True), leak) == "leaked_markup"
        assert _feed(ReplyGuard(ReplyGuardConfig(), tools=False), leak) is None
        # Thinking may mention tags; only the visible text counts.
        assert (
            _feed(ReplyGuard(ReplyGuardConfig(), tools=True), "</think> " * 3, reasoning=True)
            is None
        )

    def test_thinking_past_the_cap_trips(self) -> None:
        guard = ReplyGuard(ReplyGuardConfig(max_reasoning_chars=500))
        assert (
            _feed(guard, _varied("Let me think about the layout.", 30), reasoning=True)
            == "thinking_cap"
        )

    def test_everything_off_never_trips(self) -> None:
        cfg = ReplyGuardConfig(junk=False, repetition=False, leaked_markup=False)
        assert _feed(ReplyGuard(cfg), _salad(30) + "448" * 80) is None


# ── through the loop ────────────────────────────────────────────────────


class _BodyGateway(_Gateway):
    """A _Gateway that also keeps each request body."""

    def __init__(self, reply: Any) -> None:
        super().__init__(reply)
        self.bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.requests += 1
        return httpx.Response(200, headers=_SSE, content=self._reply(self.requests))


def _reasoning(text: str) -> bytes:
    return _event({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": text}}]})


def _guarded_config(guard: ReplyGuardConfig | None) -> ModelConfig:
    config = make_config()
    for preset in config.presets.values():
        for name in ("brain", "worker", "validator"):
            tier = getattr(preset, name)
            setattr(preset, name, tier.model_copy(update={"reply_guard": guard}))
    config.provider_retry = ProviderRetryConfig(max_retries=2, initial_delay=0.001, max_delay=0.001)
    return config


async def _run(
    tmp_path: Path, gateway: _Gateway, guard: ReplyGuardConfig | None
) -> tuple[Session, TurnComplete]:
    session = Session(str(tmp_path))
    client = gateway.client(retries=2)
    config = _guarded_config(guard)

    async def factory(*_args: object) -> ProviderClient:
        return client

    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(),
            factory,
            config,
            tool_dispatcher=attach_auto_approver(ToolDispatcher(ToolRegistry())),
        ),
    )
    await runner.start()
    try:
        await session.add_user_message("build it")
        turn = await wait_for_turn(session, 1)
    finally:
        await runner.cancel()
        await client.close()
    return session, turn


def _retracts(session: Session) -> list[AssistantRetract]:
    return [e for e in session.event_log.all_events if isinstance(e, AssistantRetract)]


class TestLoop:
    async def test_a_junk_reply_is_withdrawn_and_asked_again(self, tmp_path: Path) -> None:
        gateway = _Gateway(
            lambda attempt: _text("Plan: " + _salad(40)) if attempt == 1 else _answer("Done.")
        )
        session, turn = await _run(tmp_path, gateway, ReplyGuardConfig())

        assert (turn.failed, turn.error_code) == (False, None)
        assert gateway.requests == 2
        (retract,) = _retracts(session)
        assert retract.reason == GUARDED_CODE
        assert "".join(shown_deltas(session.event_log.all_events)) == "Done."

    async def test_without_the_guard_the_junk_is_the_answer(self, tmp_path: Path) -> None:
        gateway = _Gateway(lambda attempt: _answer("Plan: " + _salad(40)))
        session, turn = await _run(tmp_path, gateway, None)

        assert not turn.failed and gateway.requests == 1 and not _retracts(session)

    async def test_retries_run_out_and_the_turn_says_why(self, tmp_path: Path) -> None:
        gateway = _Gateway(lambda attempt: _text("x" + "448" * 100))
        _, turn = await _run(tmp_path, gateway, ReplyGuardConfig(retries=1))

        assert turn.failed and turn.error_code == GUARDED_CODE
        assert gateway.requests == 2

    async def test_after_runaway_thinking_the_retry_sends_retry_extra_body(
        self, tmp_path: Path
    ) -> None:
        gateway = _BodyGateway(
            lambda attempt: (
                _reasoning(_varied("Thinking about it.", 60)) if attempt == 1 else _answer("Done.")
            )
        )
        guard = ReplyGuardConfig(
            max_reasoning_chars=400,
            retry_extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        _, turn = await _run(tmp_path, gateway, guard)

        assert not turn.failed and gateway.requests == 2
        assert "chat_template_kwargs" not in gateway.bodies[0]
        assert gateway.bodies[1]["chat_template_kwargs"] == {"enable_thinking": False}
