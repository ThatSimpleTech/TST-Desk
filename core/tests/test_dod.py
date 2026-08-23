"""Definition-of-done polling (TD-4103).

Covers: ``$`` items run as shell through the dispatcher; prose items
go to the worker as GREEN/RED; all green stops and notifies; a red
item is not an instant stop.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_autonomy_loop import _wire_autonomy, make_charter
from tests.test_cap_enforcement import wait_for_state
from tests.test_dispatch import make_config, wait_for_turn
from tests.test_loop import start_loop as start_text_loop
from tests.test_shell_tools import make_shell_dispatcher
from tstd.autonomy.dod import (
    DodItemResult,
    DodPoll,
    command_from_dod,
    make_dod_poller,
    parse_dod_verdict,
    poll_definition_of_done,
    shell_exit_zero,
)
from tstd.autonomy.runner import (
    DOD_MET,
    advance_autonomy,
    first_prompt,
    should_notify,
)
from tstd.mock import MockProvider, Script
from tstd.protocol import TurnComplete
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools.results import ToolResult


class RecordingDispatcher:
    """Captures shell dispatches without spawning a process."""

    def __init__(self, output: str, status: str = "success") -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.on_class_c = None
        self.ledger: object | None = object()
        self._output = output
        self._status = status

    async def dispatch(
        self,
        tool_call_id: str,
        name: str,
        arguments: dict[str, object],
        session: object = None,
    ) -> ToolResult:
        self.calls.append((tool_call_id, name, arguments))
        return ToolResult(
            tool_call_id=tool_call_id,
            name=name,
            status=self._status,  # type: ignore[arg-type]
            output=self._output,
        )


# ── Parsing ──────────────────────────────────────────────────────────────


class TestDodParse:
    def test_dollar_prefix_is_a_command(self) -> None:
        assert command_from_dod("$ echo ok") == "echo ok"
        assert command_from_dod("  $cargo test  ") == "cargo test"
        assert command_from_dod("$") is None

    def test_prose_is_not_a_command(self) -> None:
        assert command_from_dod("cargo test passes") is None
        assert command_from_dod("The suite is green") is None

    def test_verdict_reads_green_or_red(self) -> None:
        assert parse_dod_verdict("GREEN") is True
        assert parse_dod_verdict("  red.  ") is False
        assert parse_dod_verdict("Hello from mock provider") is None

    def test_exit_zero_is_its_own_line(self) -> None:
        assert shell_exit_zero("exit code: 0\n── stdout ──")
        assert not shell_exit_zero("exit code: 10\n── stdout ──")
        assert not shell_exit_zero("something exit code: 0 hidden")


# ── Poller ───────────────────────────────────────────────────────────────


class TestDodPoller:
    async def test_command_item_goes_through_the_dispatcher(self) -> None:
        session = Session("/tmp/ws")
        session.autonomy = True
        session.charter = make_charter(definition_of_done=["$ echo ok"])
        dispatcher = RecordingDispatcher("header\nexit code: 0\n")
        worker_prompts: list[str] = []

        async def _worker(prompt: str) -> str:
            worker_prompts.append(prompt)
            return "RED"

        session.dod_poller = make_dod_poller(session, dispatcher=dispatcher, ask_worker=_worker)
        poll = await session.dod_poller()
        assert poll.all_green
        assert poll.results[0].via == "shell"
        assert dispatcher.calls == [("dod-0", "shell", {"command": "echo ok"})]
        assert worker_prompts == []

    async def test_prose_is_not_shelled(self) -> None:
        session = Session("/tmp/ws")
        session.autonomy = True
        session.charter = make_charter(definition_of_done=["cargo test passes"])
        dispatcher = RecordingDispatcher("exit code: 0\n")

        async def _worker(_prompt: str) -> str:
            return "GREEN"

        session.dod_poller = make_dod_poller(session, dispatcher=dispatcher, ask_worker=_worker)
        poll = await session.dod_poller()
        assert poll.all_green
        assert poll.results[0].via == "worker"
        assert dispatcher.calls == []

    async def test_live_shell_through_the_gate(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.autonomy = True
        session.charter = make_charter(definition_of_done=["$ echo ok"])
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("echo",))
        dispatcher.autonomy_fn = lambda: True
        session.dod_poller = make_dod_poller(session, dispatcher=dispatcher, ask_worker=None)
        poll = await session.dod_poller()
        assert poll.all_green
        assert poll.results[0].via == "shell"

    async def test_nonzero_shell_is_red(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.autonomy = True
        session.charter = make_charter(
            definition_of_done=["$ false"],
            allowed_commands=["false"],
        )
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("false",))
        dispatcher.autonomy_fn = lambda: True
        session.dod_poller = make_dod_poller(session, dispatcher=dispatcher, ask_worker=None)
        poll = await session.dod_poller()
        assert not poll.all_green
        assert poll.results[0].via == "shell"

    async def test_class_c_command_is_red_not_a_stop(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# hi\n", encoding="utf-8")
        session = Session(str(tmp_path))
        session.autonomy = True
        session.charter = make_charter(
            definition_of_done=["$ echo rewritten > AGENTS.md"],
            allowed_commands=["echo"],
        )
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("echo",))
        dispatcher.autonomy_fn = lambda: True
        dispatcher.on_class_c = session.mark_class_c
        session.dod_poller = make_dod_poller(session, dispatcher=dispatcher, ask_worker=None)
        poll = await session.dod_poller()
        assert not poll.all_green
        assert not session.autonomy_class_c
        assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "# hi\n"

    async def test_mixed_is_not_all_green(self) -> None:
        async def _shell(_command: str) -> tuple[bool, str]:
            return True, "exit code: 0"

        async def _worker(_item: str) -> tuple[bool, str]:
            return False, "RED"

        poll = await poll_definition_of_done(
            ["$ echo ok", "The suite is green"],
            run_shell=_shell,
            ask_worker=_worker,
        )
        assert not poll.all_green
        assert poll.results[0].green
        assert not poll.results[1].green


# ── Scheduler ────────────────────────────────────────────────────────────


class TestDodAdvance:
    async def test_all_green_stops_and_notifies(self) -> None:
        session = Session("/tmp/ws")
        session.autonomy = True
        session.charter = make_charter(max_iterations=8)
        notified: list[str] = []

        async def _notify(message: str) -> None:
            notified.append(message)

        session.autonomy_notify = _notify

        async def _green() -> DodPoll:
            return DodPoll((DodItemResult("The suite is green", True, "GREEN", "worker"),))

        session.dod_poller = _green
        assert await advance_autonomy(session) is False
        assert session.autonomy_stop_reason == DOD_MET
        assert notified == [DOD_MET]
        assert should_notify(DOD_MET)

    async def test_any_red_does_not_stop(self) -> None:
        session = Session("/tmp/ws")
        session.autonomy = True
        session.charter = make_charter(max_iterations=8)

        async def _red() -> DodPoll:
            return DodPoll((DodItemResult("The suite is green", False, "RED", "worker"),))

        session.dod_poller = _red
        assert await advance_autonomy(session) is True
        assert session.autonomy_stop_reason is None
        assert session.autonomy_turns == 1

    async def test_poller_failure_is_red(self) -> None:
        session = Session("/tmp/ws")
        session.autonomy = True
        session.charter = make_charter(max_iterations=8)

        async def _boom() -> DodPoll:
            raise RuntimeError("worker down")

        session.dod_poller = _boom
        assert await advance_autonomy(session) is True
        assert session.autonomy_stop_reason is None


# ── Loop ─────────────────────────────────────────────────────────────────


class TestDodLoop:
    async def test_all_green_completes_and_notifies(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        charter = make_charter(max_iterations=8)
        notified = _wire_autonomy(session, charter)
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="Working"),
                "test-worker": Script(kind="text", content="GREEN"),
            }
        )
        runner = await start_text_loop(session, TierRouter(), mock, make_config())
        await session.add_user_message(first_prompt(charter))
        await wait_for_state(session, "complete")
        assert session.autonomy_stop_reason == DOD_MET
        assert notified == [DOD_MET]
        completes = [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]
        assert len(completes) == 1
        assert not runner.is_running

    async def test_red_is_not_an_instant_stop(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        charter = make_charter(max_iterations=2)
        notified = _wire_autonomy(session, charter)
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="Working"),
                "test-worker": Script(kind="text", content="RED"),
            }
        )
        runner = await start_text_loop(session, TierRouter(), mock, make_config())
        await session.add_user_message(first_prompt(charter))
        await wait_for_turn(session, 2)
        await wait_for_state(session, "complete")
        assert session.autonomy_stop_reason is not None
        assert session.autonomy_stop_reason.startswith("iteration cap")
        assert notified
        assert DOD_MET not in notified
        assert not runner.is_running

    async def test_interactive_session_does_not_poll(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="ok"),
                "test-worker": Script(kind="text", content="GREEN"),
            }
        )
        runner = await start_text_loop(session, TierRouter(), mock, make_config())
        await session.add_user_message("hello")
        await wait_for_turn(session, 1)
        assert session.dod_poller is None
        assert session.state == "running"
        assert [c.model for c in mock.calls] == ["test-brain"]
        await runner.cancel()
