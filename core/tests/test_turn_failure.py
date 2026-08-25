"""Tests for the failed-turn wire shape (TD-1008).

Notifications key tailored copy off typed causes, so a failed turn must be
machine-visible: ``turn_complete`` carries ``failed`` + ``error_code``, the
provider's typed code survives to the wire, and a missing keychain key fails
the *turn* (not the session) with ``missing_api_key`` so the conversation
survives until the user stores a key.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_dispatch import (
    attach_auto_approver,
    make_config,
    start_loop,
    wait_for_turn,
)
from tstd.keychain import KeychainError
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import AssistantDelta, TurnComplete
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import ToolDispatcher, ToolRegistry


def make_bare_session(workspace: Path) -> tuple[Session, ToolDispatcher]:
    """A session with an empty tool registry (failed turns never dispatch)."""
    session = Session(str(workspace))
    dispatcher = attach_auto_approver(ToolDispatcher(ToolRegistry()))
    return session, dispatcher


def turn_events(session: Session) -> list[TurnComplete]:
    return [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]


def assistant_text(session: Session) -> str:
    """Everything the transcript would render as assistant output.

    The event log is what the chat pane draws from, live and on replay, so a
    message that exists only in the persisted model conversation is a message
    the user never sees.
    """
    return "".join(e.delta for e in session.event_log.all_events if isinstance(e, AssistantDelta))


class TestFailedTurnWire:
    async def test_provider_error_code_reaches_turn_complete(self, tmp_path: Path) -> None:
        session, dispatcher = make_bare_session(tmp_path)
        mock = MockProvider(
            sequences={
                slug: [
                    Script(
                        kind="error",
                        error_code="auth_failed",
                        status_code=401,
                        content="Authentication failed.",
                    )
                ]
                for slug in ("test-brain", "test-worker", "test-validator")
            }
        )
        await start_loop(session, TierRouter(), mock, make_config(), None, dispatcher)

        await session.add_user_message("hi")
        turn = await wait_for_turn(session, 1)

        assert turn.failed is True
        assert turn.error_code == "auth_failed"

        # The failure stays a *turn* failure: the session is not dead and
        # the chat log carries the actionable message.
        assert session.state != "failed"
        assert session.state != "cancelled"

        # ...and "carries" means on the wire, not just in the persisted
        # conversation. A failed turn that emits no delta leaves the chat pane
        # blank, which reads as an app that stopped responding rather than one
        # that hit a provider error.
        assert "Authentication failed." in assistant_text(session)

    async def test_failed_turn_is_visible_in_the_transcript(self, tmp_path: Path) -> None:
        """A provider refusal reaches the chat pane, not just the log file."""
        session, dispatcher = make_bare_session(tmp_path)
        mock = MockProvider(
            sequences={
                slug: [
                    Script(
                        kind="error",
                        error_code="insufficient_credits",
                        status_code=402,
                        content="Insufficient credits. Add more using https://example.test/credits",
                    )
                ]
                for slug in ("test-brain", "test-worker", "test-validator")
            }
        )
        await start_loop(session, TierRouter(), mock, make_config(), None, dispatcher)

        await session.add_user_message("hi")
        turn = await wait_for_turn(session, 1)

        assert turn.failed is True
        assert turn.error_code == "insufficient_credits"

        # The provider's own actionable wording survives to the user — the
        # whole point of a typed cause is that the fix is nameable.
        shown = assistant_text(session)
        assert "Insufficient credits" in shown
        assert "https://example.test/credits" in shown

    async def test_successful_turn_marks_failed_false(self, tmp_path: Path) -> None:
        session, dispatcher = make_bare_session(tmp_path)
        mock = MockProvider(
            sequences={
                slug: [Script(kind="stream", content="ok")]
                for slug in ("test-brain", "test-worker", "test-validator")
            }
        )
        await start_loop(session, TierRouter(), mock, make_config(), None, dispatcher)

        await session.add_user_message("hi")
        turn = await wait_for_turn(session, 1)

        assert turn.failed is False
        assert turn.error_code is None

    async def test_missing_key_fails_turn_not_session(self, tmp_path: Path) -> None:
        session, dispatcher = make_bare_session(tmp_path)

        async def no_key_factory() -> MockProvider:
            raise KeychainError("API key not found in keychain.")

        # start_loop wraps a MockProvider; here the provider itself is the
        # failure, so the runner is built directly around the real loop.
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s,
                TierRouter(),
                no_key_factory,
                make_config(),
                tool_dispatcher=dispatcher,
            ),
        )
        await runner.start()

        await session.add_user_message("hi")
        turn = await wait_for_turn(session, 1)

        assert turn.failed is True
        assert turn.error_code == "missing_api_key"
        # The session survives: once the key exists, the next message retries.
        assert session.state == "running"
        # And the user is told why, in the chat, rather than watching a turn
        # end with no output at all.
        assert "API key not found in keychain." in assistant_text(session)

        await runner.cancel()
