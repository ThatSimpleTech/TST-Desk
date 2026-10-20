"""Definition-of-done polling for unattended runs (TD-4103).

After each finished autonomy turn the scheduler asks whether every
charter item is met.  A ``$`` prefix is an explicit shell command and
runs through the dispatcher (classifier + policy).  Everything else is
prose and is judged GREEN/RED by the worker — spec §12.4 examples are
English, not argv.  All green stops the run; any red continues.  A red
streak is TD-4203.

The interactive done-check (TD-5124, ``tstd/stop_check.py``) polls its
items through the same :func:`poll_definition_of_done`. Its runners may
answer ``None`` for an item they could not judge (the person declined
the command, the worker did not answer); the item is then red and
``decided`` is false. The unattended runners here never do, so a
charter's poll still fails toward red.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..provider import ChatMessage, content_as_text
from ..tools.results import ToolResult

if TYPE_CHECKING:
    from ..session import Session
    from ..tools.dispatch import ToolDispatcher

DOD_INSTRUCTION = (
    "Decide whether this definition-of-done item is already met.\nReply with exactly GREEN or RED."
)

_LAST_ASSISTANT_CHARS = 500
_DETAIL_CHARS = 500


@dataclass(frozen=True)
class DodItemResult:
    """One charter item after a poll."""

    item: str
    green: bool
    detail: str
    via: Literal["shell", "worker"]
    # False when the runner could not judge the item (TD-5124): it is red,
    # but nothing about the work was learnt.
    decided: bool = True


@dataclass(frozen=True)
class DodPoll:
    """Every charter item's verdict for one iteration."""

    results: tuple[DodItemResult, ...]

    @property
    def all_green(self) -> bool:
        return bool(self.results) and all(item.green for item in self.results)


def command_from_dod(item: str) -> str | None:
    """Return the shell command if *item* is an explicit ``$`` command."""
    text = item.strip()
    if not text.startswith("$"):
        return None
    command = text[1:].strip()
    return command or None


def parse_dod_verdict(text: str) -> bool | None:
    """Read GREEN/RED from a worker reply.  Anything else is ``None``."""
    stripped = text.strip().strip(".:\"'`")
    if not stripped:
        return None
    token = stripped.split()[0].upper()
    if token == "GREEN":
        return True
    if token == "RED":
        return False
    return None


def shell_exit_zero(output: str) -> bool:
    """True only when the shell result reports exit code 0 as its own line."""
    return any(line.strip() == "exit code: 0" for line in output.splitlines())


#: A runner's verdict on one item: green, red, or ``None`` when it could
#: not judge it (TD-5124), and the detail to show.
ItemRunner = Callable[[str], Awaitable[tuple[bool | None, str]]]


def _item_result(
    item: str, verdict: tuple[bool | None, str], via: Literal["shell", "worker"]
) -> DodItemResult:
    green, detail = verdict
    return DodItemResult(item, green is True, detail, via, decided=green is not None)


async def poll_definition_of_done(
    items: Sequence[str],
    *,
    run_shell: ItemRunner | None,
    ask_worker: ItemRunner | None,
) -> DodPoll:
    """Judge each *item*.  Missing runners fail toward red, never green."""
    results: list[DodItemResult] = []
    for item in items:
        command = command_from_dod(item)
        if command is not None:
            if run_shell is None:
                results.append(DodItemResult(item, False, "no shell runner", "shell", False))
                continue
            results.append(_item_result(item, await run_shell(command), "shell"))
            continue
        if ask_worker is None:
            results.append(DodItemResult(item, False, "no worker runner", "worker", False))
            continue
        results.append(_item_result(item, await ask_worker(item), "worker"))
    return DodPoll(tuple(results))


def last_assistant_text(conversation: Sequence[ChatMessage]) -> str:
    """The newest assistant message with content, as text; empty when none."""
    for message in reversed(conversation):
        if message.role == "assistant" and message.content:
            return content_as_text(message.content)
    return ""


def dod_worker_prompt(item: str, objective: str, last_assistant: str) -> str:
    """The worker's GREEN/RED question for one prose item."""
    return (
        f"{DOD_INSTRUCTION}\n\n"
        f"Objective: {objective}\n"
        f"Item: {item}\n"
        f"Last assistant: {last_assistant[:_LAST_ASSISTANT_CHARS]}\n"
        "Verdict:"
    )


async def dispatch_check_command(
    dispatcher: ToolDispatcher,
    tool_call_id: str,
    arguments: dict[str, object],
    session: Session,
) -> ToolResult:
    """Run a check's ``$`` command through the dispatcher's gate.

    A poll is TST Desk asking, not an agent decision: it neither
    Class-C-stops an unattended run nor appends to the decision ledger.
    The classifier, policy and approval still decide whether it runs.
    """
    previous_class_c = dispatcher.on_class_c
    previous_ledger = dispatcher.ledger
    dispatcher.on_class_c = None
    dispatcher.ledger = None
    try:
        return await dispatcher.dispatch(tool_call_id, "shell", arguments, session=session)
    finally:
        dispatcher.on_class_c = previous_class_c
        dispatcher.ledger = previous_ledger


def make_dod_poller(
    session: Session,
    *,
    dispatcher: ToolDispatcher | None,
    ask_worker: Callable[[str], Awaitable[str]] | None,
) -> Callable[[], Awaitable[DodPoll]]:
    """Build a poller that uses this session's dispatcher and worker."""

    async def _run_shell(command: str) -> tuple[bool, str]:
        if dispatcher is None:
            return False, "no dispatcher"
        try:
            result = await dispatch_check_command(
                dispatcher, f"dod-{session.autonomy_turns}", {"command": command}, session
            )
        except Exception as exc:
            return False, str(exc)
        if result.status != "success":
            return False, result.output[:_DETAIL_CHARS]
        return shell_exit_zero(result.output), result.output[:_DETAIL_CHARS]

    async def _ask_worker(item: str) -> tuple[bool, str]:
        if ask_worker is None:
            return False, "no worker"
        objective = session.charter.objective if session.charter is not None else ""
        prompt = dod_worker_prompt(item, objective, last_assistant_text(session.conversation))
        try:
            text = await ask_worker(prompt)
        except Exception as exc:
            return False, str(exc)
        verdict = parse_dod_verdict(text)
        if verdict is None:
            return False, text[:_DETAIL_CHARS]
        return verdict, text[:_DETAIL_CHARS]

    async def _poll() -> DodPoll:
        charter = session.charter
        if charter is None:
            return DodPoll(())
        return await poll_definition_of_done(
            charter.definition_of_done,
            run_shell=_run_shell,
            ask_worker=_ask_worker,
        )

    return _poll
