"""Done-check on interactive turns (TD-5124).

Local models often stop early, or say they are done when they are not.
A workspace can name a ``stop_check`` in ``.tst/config.yaml``, and
``/goal <statement>`` gives a session a goal (``session_goal.py``). When
the model ends an interactive turn, both are checked with the unattended runs' DoD code
(``autonomy/dod.py``): a ``$`` item runs as a shell command through the
dispatcher (classifier, policy, approval), anything else is a statement
the worker judges GREEN/RED. No charter is involved.

- All green: the turn ends. A goal that was checked is met and cleared.
- Some red: the failure goes back to the model as a user message and the
  turn goes on, up to ``max_retries`` times; after that the turn ends with
  ``stop_check_failed`` and the failure shown. A red item wins over one
  that could not be judged: the red one goes back, the other is left out.
- No red, but an item that could not be judged (a declined command, no
  verdict): the turn ends and nothing goes back, since the model could
  not fix that.

Each check logs a ``stop_check`` event; a ``$`` item's command logs its
own ``tool_call`` and ``tool_result``, so the window shows it running and
its approval card clears. Unattended runs keep their charter's DoD poll,
and delegate children are not checked.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Literal

from .autonomy.dod import (
    DodItemResult,
    DodPoll,
    dispatch_check_command,
    dod_worker_prompt,
    last_assistant_text,
    parse_dod_verdict,
    poll_definition_of_done,
    shell_exit_zero,
)
from .hooks import MAX_STOP_BLOCKS
from .logging import get_logger
from .protocol import StopCheck, StopCheckItem, StopCheckOutcome
from .protocol import ToolCall as ToolCallEvent
from .protocol import ToolResult as ToolResultEvent
from .provider import ChatMessage, content_as_text
from .session_goal import set_goal
from .tools.dispatch import build_decision_request

if TYPE_CHECKING:
    from .session import Session
    from .tools import ToolDispatcher

log = get_logger(__name__)

#: How much of a failed item's output goes back to the model and the window.
DETAIL_CHARS = 2000
_DETAIL_HEAD = 300
_OBJECTIVE_CHARS = 500

Source = Literal["workspace", "goal"]
TurnEnd = Literal["end", "retry", "failed", "stopped"]


# ── The check ─────────────────────────────────────────────────────────────


def clip_detail(text: str, limit: int = DETAIL_CHARS) -> str:
    """*text* within *limit*: its start and, mostly, its end.

    A test run says what failed at the bottom, so the tail is kept.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    marker = "\n…\n"
    tail = limit - _DETAIL_HEAD - len(marker)
    return text[:_DETAIL_HEAD] + marker + text[-tail:]


def _turn_request(conversation: Sequence[ChatMessage]) -> str:
    """The user message that started the turn, for the worker's objective."""
    for message in reversed(conversation):
        if message.role == "user" and not message.steer:
            return content_as_text(message.content)
    return ""


def outcome_for(poll: DodPoll, attempt: int, max_retries: int) -> StopCheckOutcome:
    """``passed``, ``retry``, ``failed`` or ``unchecked`` for one poll.

    A red item decides the outcome even when another item could not be
    judged: the model can fix the red one, so it goes back (or, out of
    tries, fails the turn). ``unchecked`` is only for a poll with no red
    item and at least one that could not be judged.
    """
    if any(result.decided and not result.green for result in poll.results):
        return "retry" if attempt <= max_retries else "failed"
    if poll.all_green:
        return "passed"
    return "unchecked"


def feedback_text(
    results: Sequence[tuple[DodItemResult, Source]], attempt: int, max_retries: int
) -> str:
    """The user message a failed check sends back to the model.

    Only items judged red are named: one that could not be judged (a
    declined command, no verdict) is nothing the model can fix.
    """
    lines = [
        f"The done-check failed (try {attempt} of {max_retries}), so the work is not "
        "finished. Fix what it reports, then end your turn again.",
    ]
    for result, source in results:
        if result.green or not result.decided:
            continue
        if result.via == "shell":
            # A `$` goal runs as a command too (TD-5161): its output is what
            # the model needs to fix it.
            label = "Goal not met yet: " if source == "goal" else ""
            output = clip_detail(result.detail)
            lines.append(f"- {label}`{result.item}` failed:\n```\n{output}\n```")
        elif source == "goal":
            lines.append(f"- Goal not met yet: {result.item}")
        else:
            lines.append(f"- Not true yet: {result.item}")
    return "\n".join(lines)


class TurnStopCheck:
    """One interactive turn's done-check and the tries it has used."""

    def __init__(
        self,
        session: Session,
        dispatcher: ToolDispatcher | None,
        ask_worker: Callable[[str], Awaitable[str]] | None,
    ) -> None:
        self.session = session
        self.dispatcher = dispatcher
        self.ask_worker = ask_worker
        self.attempts = 0

    def items(self) -> list[tuple[str, Source]]:
        """The workspace's checks, then the session's goal."""
        session = self.session
        if session.autonomy or session.delegate_depth >= 1:
            return []
        # TD-5123: a planning turn is read-only research that ends in a plan
        # to review, not a claim the work is done. Checking it would send
        # the model back to fix what plan mode will not let it touch. The
        # goal stays set, and the first turn after plan mode is checked.
        router = session.router
        if router is not None and router.plan_mode:
            return []
        found: list[tuple[str, Source]] = [
            (check, "workspace") for check in session.boundary_config.stop_check.checks
        ]
        if session.goal is not None:
            found.append((session.goal, "goal"))
        return found

    async def at_turn_end(self, messages: list[ChatMessage]) -> TurnEnd:
        """Check the turn the model just ended.

        ``retry`` means the feedback is on *messages* and the turn goes on;
        ``failed`` ends it with the failure shown; ``stopped`` means Stop or
        cancel landed during the check.
        """
        hooked = await self._stop_hooks(messages)
        if hooked is not None:
            return hooked
        items = self.items()
        if not items:
            return "end"
        self.attempts += 1
        cfg = self.session.boundary_config.stop_check
        poll = await poll_definition_of_done(
            [item for item, _ in items],
            run_shell=self._run_shell,
            ask_worker=self._ask_worker,
        )
        session = self.session
        if session.cancel_requested or session.stop_turn_requested:
            return "stopped"
        results = list(zip(poll.results, [source for _, source in items], strict=True))
        outcome = outcome_for(poll, self.attempts, cfg.max_retries)
        if session.goal is not None and any(
            source == "goal" and result.decided for result, source in results
        ):
            session.goal_checks += 1
        await session.event_log.add(
            StopCheck(
                session_id=session.id,
                outcome=outcome,
                attempt=self.attempts,
                max_retries=cfg.max_retries,
                items=[
                    StopCheckItem(
                        item=result.item,
                        source=source,
                        via=result.via,
                        passed=result.green,
                        decided=result.decided,
                        detail=clip_detail(result.detail),
                    )
                    for result, source in results
                ],
                seq=1,
            )
        )
        log.info(
            "stop check",
            extra={
                "extra_fields": {
                    "session_id": session.id,
                    "outcome": outcome,
                    "attempt": self.attempts,
                    "items": len(results),
                }
            },
        )
        if outcome == "passed":
            if session.goal is not None and any(source == "goal" for _, source in items):
                await set_goal(session, None, "met")
            return "end"
        if outcome == "retry":
            messages.append(
                ChatMessage(
                    role="user",
                    content=feedback_text(results, self.attempts, cfg.max_retries),
                    steer=True,
                    check=True,
                )
            )
            await session.conversation_changed()
            return "retry"
        return "failed" if outcome == "failed" else "end"

    async def _stop_hooks(self, messages: list[ChatMessage]) -> TurnEnd | None:
        """Run ``stop`` hooks (TD-5134); a block sends the model back to work.

        Up to :data:`MAX_STOP_BLOCKS` times a turn, so a hook that always
        blocks cannot keep a turn going forever. ``None`` lets the
        done-check go on.
        """
        session = self.session
        if session.delegate_depth >= 1:
            return None
        hooks = session.hooks
        verdict = await hooks.run("stop", {"last_message": last_assistant_text(messages)})
        if session.cancel_requested or session.stop_turn_requested:
            return "stopped"
        if verdict.block is None or hooks.stop_blocks >= MAX_STOP_BLOCKS:
            return None
        hooks.stop_blocks += 1
        messages.append(
            ChatMessage(
                role="user",
                content=(
                    f"A stop hook says the work is not finished (try {hooks.stop_blocks} of "
                    f"{MAX_STOP_BLOCKS}): {verdict.block}\n"
                    "Deal with that, then end your turn again."
                ),
                steer=True,
                check=True,
            )
        )
        await session.conversation_changed()
        return "retry"

    async def _run_shell(self, command: str) -> tuple[bool | None, str]:
        dispatcher = self.dispatcher
        session = self.session
        if dispatcher is None:
            return None, "This session has no tools, so the command could not run."
        call_id = f"check-{uuid.uuid4().hex[:12]}"
        arguments: dict[str, object] = {
            "command": command,
            "timeout_secs": session.boundary_config.stop_check.timeout_secs,
        }
        await session.event_log.add(
            ToolCallEvent(
                session_id=session.id,
                tool_call_id=call_id,
                name="shell",
                arguments=dict(arguments),
                decision_class=await self._event_class(dispatcher, arguments),
                seq=1,
            )
        )
        try:
            result = await dispatch_check_command(dispatcher, call_id, arguments, session)
        except Exception as exc:
            log.exception(
                "stop check command failed",
                extra={"extra_fields": {"session_id": session.id}},
            )
            await self._log_result(call_id, "error", f"The check could not run: {exc}", None)
            return None, f"The check could not run: {exc}"
        await self._log_result(call_id, result.status, result.output, result.error_code)
        # An error result is a command that never reached an exit code: it
        # was declined, refused by policy or the allowlist, or failed to
        # start. That says nothing about the work.
        if result.status != "success":
            return None, result.output
        return shell_exit_zero(result.output), result.output

    async def _event_class(
        self, dispatcher: ToolDispatcher, arguments: dict[str, object]
    ) -> Literal["A", "B", "C"] | None:
        tool = dispatcher.registry.get("shell")
        if tool is None or dispatcher.classifier is None:
            return None
        found = await dispatcher.classifier.classify(build_decision_request(tool, arguments))
        return None if found.decision_class is None else found.decision_class.value

    async def _log_result(
        self, call_id: str, status: Literal["success", "error"], output: str, code: str | None
    ) -> None:
        await self.session.event_log.add(
            ToolResultEvent(
                session_id=self.session.id,
                tool_call_id=call_id,
                status=status,
                output=output,
                error_code=code,
                seq=1,
            )
        )

    async def _ask_worker(self, item: str) -> tuple[bool | None, str]:
        if self.ask_worker is None:
            return None, "No worker model is set up to judge this."
        conversation = self.session.conversation
        prompt = dod_worker_prompt(
            item,
            _turn_request(conversation)[:_OBJECTIVE_CHARS],
            last_assistant_text(conversation),
        )
        try:
            reply = await self.ask_worker(prompt)
        except Exception as exc:
            log.warning(
                "stop check worker call failed",
                extra={"extra_fields": {"session_id": self.session.id, "error": str(exc)}},
            )
            return None, f"The worker model did not answer: {exc}"
        return parse_dod_verdict(reply), reply.strip() or "(no reply)"
