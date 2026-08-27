"""Definition-of-done polling for unattended runs (TD-4103).

After each finished autonomy turn the scheduler asks whether every
charter item is met.  A ``$`` prefix is an explicit shell command and
runs through the dispatcher (classifier + policy).  Everything else is
prose and is judged GREEN/RED by the worker — spec §12.4 examples are
English, not argv.  All green stops the run; any red continues.  A red
streak is TD-4203.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..tools.results import ToolResult
from ..provider import content_as_text

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


async def poll_definition_of_done(
    items: Sequence[str],
    *,
    run_shell: Callable[[str], Awaitable[tuple[bool, str]]] | None,
    ask_worker: Callable[[str], Awaitable[tuple[bool, str]]] | None,
) -> DodPoll:
    """Judge each *item*.  Missing runners fail toward red, never green."""
    results: list[DodItemResult] = []
    for item in items:
        command = command_from_dod(item)
        if command is not None:
            if run_shell is None:
                results.append(DodItemResult(item, False, "no shell runner", "shell"))
                continue
            green, detail = await run_shell(command)
            results.append(DodItemResult(item, green, detail, "shell"))
            continue
        if ask_worker is None:
            results.append(DodItemResult(item, False, "no worker runner", "worker"))
            continue
        green, detail = await ask_worker(item)
        results.append(DodItemResult(item, green, detail, "worker"))
    return DodPoll(tuple(results))


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
        previous_class_c = dispatcher.on_class_c
        previous_ledger = dispatcher.ledger
        # A poll is the scheduler asking, not an agent decision: do not
        # Class-C-stop the run, and do not append to the ledger.
        dispatcher.on_class_c = None
        dispatcher.ledger = None
        try:
            result: ToolResult = await dispatcher.dispatch(
                f"dod-{session.autonomy_turns}",
                "shell",
                {"command": command},
                session=session,
            )
        except Exception as exc:
            return False, str(exc)
        finally:
            dispatcher.on_class_c = previous_class_c
            dispatcher.ledger = previous_ledger
        if result.status != "success":
            return False, result.output[:_DETAIL_CHARS]
        return shell_exit_zero(result.output), result.output[:_DETAIL_CHARS]

    async def _ask_worker(item: str) -> tuple[bool, str]:
        if ask_worker is None:
            return False, "no worker"
        last = ""
        for message in reversed(session.conversation):
            if message.role == "assistant" and message.content:
                last = content_as_text(message.content)
                break
        objective = session.charter.objective if session.charter is not None else ""
        prompt = (
            f"{DOD_INSTRUCTION}\n\n"
            f"Objective: {objective}\n"
            f"Item: {item}\n"
            f"Last assistant: {last[:_LAST_ASSISTANT_CHARS]}\n"
            "Verdict:"
        )
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
