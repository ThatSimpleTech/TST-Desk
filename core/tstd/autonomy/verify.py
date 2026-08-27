"""Interactive verify after writes (TD-4204).

Spec §12.6 first sentence: in interactive mode the validator reviews diffs.
This is not the autonomy supervisor (TD-4201). No charter, no auto-revert.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from ..config import ModelConfig, TierConfig
from ..context import PromptAssembler
from ..cost import CostTracker
from ..logging import get_logger
from ..protocol import VerifyResult
from ..provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ProviderError,
    content_as_text,
)

log = get_logger(__name__)

WRITE_TOOLS = frozenset({"fs_write", "fs_edit"})
VerifyMode = Literal["off", "after_write", "ask"]
VerifyVerdict = Literal["pass", "fail", "error"]

_ATTR_WROTE = "wrote_this_turn"
_ATTR_DIFFS = "verify_diffs"
_ATTR_TESTS = "verify_test_outputs"
_ATTR_PENDING = "verify_pending"
_ATTR_RESUME = "verify_resume"
_ATTR_DENY = "verify_deny"


class CompletionClient(Protocol):
    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError: ...


ClientFor = Callable[[TierConfig], Awaitable[CompletionClient]]

_REVIEW_USER = (
    "Review the workspace diff and any test output. "
    "Reply with a first line of `verdict: pass` or `verdict: fail`, "
    "then a short summary. Do not propose a revert."
)


class VerifySession(Protocol):
    """Session surface verify reads. Extra flags are set via setattr."""

    id: str
    autonomy: bool
    workspace_path: str
    event_log: Any


def clear_turn_writes(session: object) -> None:
    """Clear the per-turn write flag. Called at the start of each turn."""
    setattr(session, _ATTR_WROTE, False)
    setattr(session, _ATTR_DIFFS, [])
    setattr(session, _ATTR_TESTS, [])


def note_tool_result(
    session: object,
    name: str,
    status: str,
    *,
    diff: str | None = None,
    output: str = "",
) -> None:
    """Record a successful workspace write or a shell result from this turn.

    Filesystem write tools (``fs_write`` / ``fs_edit``) mark the turn as a
    write. A shell that only echoes is not a write; its output is kept as
    test text if a later write on the same turn triggers verify.
    """
    if status != "success":
        return
    if name in WRITE_TOOLS:
        setattr(session, _ATTR_WROTE, True)
        if diff:
            diffs = list(getattr(session, _ATTR_DIFFS, []))
            diffs.append(diff)
            setattr(session, _ATTR_DIFFS, diffs)
        return
    if name == "shell" and output:
        tests = list(getattr(session, _ATTR_TESTS, []))
        tests.append(output)
        setattr(session, _ATTR_TESTS, tests)


def wrote_this_turn(session: object) -> bool:
    """True when a filesystem write tool succeeded this turn."""
    return bool(getattr(session, _ATTR_WROTE, False))


def turn_diff(session: object) -> str:
    """Unified diffs collected from successful writes this turn."""
    return "\n\n".join(getattr(session, _ATTR_DIFFS, []))


def turn_test_output(session: object) -> str:
    """Shell output collected this turn, used as validator test text."""
    return "\n\n".join(getattr(session, _ATTR_TESTS, []))


def parse_verdict(text: str) -> tuple[VerifyVerdict, str]:
    """Read pass/fail/error from a validator reply."""
    summary = text.strip()
    if not summary:
        return "error", "Validator returned no review."
    lowered = summary.lower()
    if "verdict: pass" in lowered or lowered.startswith("pass"):
        return "pass", summary
    if "verdict: fail" in lowered or lowered.startswith("fail"):
        return "fail", summary
    if "verdict: error" in lowered:
        return "error", summary
    return "pass", summary


def bind_ask_handlers(
    session: object,
    resume: Callable[[], Awaitable[None]],
    deny: Callable[[], Awaitable[None]],
) -> None:
    """Stash confirm/deny callbacks for ask mode (tests and later daemon)."""
    setattr(session, _ATTR_RESUME, resume)
    setattr(session, _ATTR_DENY, deny)


async def confirm_pending_verify(session: object) -> None:
    """Run a parked ask-mode verify. No-op when nothing is pending."""
    resume = getattr(session, _ATTR_RESUME, None)
    if resume is None:
        return
    await resume()


async def deny_pending_verify(session: object) -> None:
    """Skip a parked ask-mode verify. No-op when nothing is pending."""
    deny = getattr(session, _ATTR_DENY, None)
    if deny is None:
        return
    await deny()


async def maybe_verify_after_turn(
    session: VerifySession,
    config: ModelConfig,
    tracker: CostTracker,
    client_for: ClientFor,
    assembler: PromptAssembler,
) -> None:
    """After ``turn_complete`` on an interactive turn that wrote.

    ``off`` never verifies. ``after_write`` runs one validator call.
    ``ask`` emits ``verify_result`` with ``pending=true`` and waits for
    :func:`confirm_pending_verify`. Autonomy sessions are skipped.
    """
    if session.autonomy:
        clear_turn_writes(session)
        return
    if not wrote_this_turn(session):
        return
    mode: VerifyMode = config.autonomy.verify
    if mode == "off":
        clear_turn_writes(session)
        return
    if mode == "ask":
        await _park_ask(session, config, tracker, client_for, assembler)
        return
    await _run_validator(session, config, tracker, client_for, assembler)
    clear_turn_writes(session)


async def _park_ask(
    session: VerifySession,
    config: ModelConfig,
    tracker: CostTracker,
    client_for: ClientFor,
    assembler: PromptAssembler,
) -> None:
    setattr(session, _ATTR_PENDING, True)
    await session.event_log.add(
        VerifyResult(
            session_id=session.id,
            verdict="error",
            summary="Confirm to review this write.",
            cost=0.0,
            pending=True,
            seq=1,
        )
    )

    ran = False

    async def resume() -> None:
        nonlocal ran
        if ran or not getattr(session, _ATTR_PENDING, False):
            return
        ran = True
        setattr(session, _ATTR_PENDING, False)
        setattr(session, _ATTR_RESUME, None)
        setattr(session, _ATTR_DENY, None)
        await _run_validator(session, config, tracker, client_for, assembler)
        clear_turn_writes(session)

    async def deny() -> None:
        nonlocal ran
        if ran or not getattr(session, _ATTR_PENDING, False):
            return
        ran = True
        setattr(session, _ATTR_PENDING, False)
        setattr(session, _ATTR_RESUME, None)
        setattr(session, _ATTR_DENY, None)
        await session.event_log.add(
            VerifyResult(
                session_id=session.id,
                verdict="error",
                summary="Verify skipped.",
                cost=0.0,
                pending=False,
                seq=1,
            )
        )
        clear_turn_writes(session)

    bind_ask_handlers(session, resume, deny)


async def _run_validator(
    session: VerifySession,
    config: ModelConfig,
    tracker: CostTracker,
    client_for: ClientFor,
    assembler: PromptAssembler,
) -> None:
    """One validator-tier completion. Never streams into the chat transcript."""
    diff = turn_diff(session) or "(no diff)"
    test_output = turn_test_output(session) or "(no test output)"
    try:
        assembled = await assembler.assemble(
            "validator",
            diff=diff,
            test_output=test_output,
        )
    except Exception:
        log.exception(
            "verify assemble failed",
            extra={"extra_fields": {"session_id": session.id}},
        )
        await session.event_log.add(
            VerifyResult(
                session_id=session.id,
                verdict="error",
                summary="Could not assemble the validator prompt.",
                cost=0.0,
                pending=False,
                seq=1,
            )
        )
        return

    validator_cfg = config.tier("validator")
    try:
        provider = await client_for(validator_cfg)
    except Exception as e:
        log.warning(
            "verify client failed",
            extra={"extra_fields": {"session_id": session.id, "error": str(e)}},
        )
        await session.event_log.add(
            VerifyResult(
                session_id=session.id,
                verdict="error",
                summary=f"Validator client failed: {e}",
                cost=0.0,
                pending=False,
                seq=1,
            )
        )
        return

    request = ChatCompletionRequest(
        model=validator_cfg.require_slug(),
        messages=[
            ChatMessage(role="system", content=assembled.text),
            ChatMessage(role="user", content=_REVIEW_USER),
        ],
        tools=None,
        max_tokens=validator_cfg.max_output_tokens,
        temperature=0.0,
    )
    try:
        response = await provider.chat_completion(request)
    except Exception as e:
        log.warning(
            "verify call raised",
            extra={"extra_fields": {"session_id": session.id, "error": str(e)}},
        )
        await session.event_log.add(
            VerifyResult(
                session_id=session.id,
                verdict="error",
                summary=f"Validator call failed: {e}",
                cost=0.0,
                pending=False,
                seq=1,
            )
        )
        return

    cost = 0.0
    if isinstance(response, ProviderError):
        verdict: VerifyVerdict = "error"
        summary = f"Validator error: {response.message}"
    else:
        verdict, summary = parse_verdict(content_as_text(response.message.content))
        if response.usage is not None:
            cost = tracker.record_off_turn("validator", response.usage, validator_cfg)
            await session.event_log.add(tracker.emit_cost_update(session.id))

    await session.event_log.add(
        VerifyResult(
            session_id=session.id,
            verdict=verdict,
            summary=summary,
            cost=cost,
            pending=False,
            seq=1,
        )
    )
