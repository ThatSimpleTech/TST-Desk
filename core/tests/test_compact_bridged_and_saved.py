"""Compaction on request reaches other computers and survives a restart (TD-5154).

Two gaps the review of TD-5109 found. ``compact_context`` was not a
forwarded verb, so on a session that lives on another computer (TD-5015)
the window dropped it and the hub refused it. And ``Session.compact_now``
rewrote the conversation in memory only: ``conversation.json`` kept the
old one until the next turn ended, so a restart in between brought back
every turn the user had just compacted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.machine_helpers import FakeKeychain, Fleet, eventually, window
from tests.test_context_fit import _tier
from tests.test_loop import wait_for_turn
from tests.test_peer_policy import BRIDGED, MACHINE, PHONE_VIEWER, VIEWER, _msg
from tests.test_session_revive import _stop_runners
from tests.test_two_daemons import VIEWER as VIEWER_FEATURES
from tests.test_two_daemons import _for, _open, _pair, _until
from tstd.compaction import ContextMeter
from tstd.context.tokens import HeuristicTokenCounter
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.peer_policy import FORWARDED_VERBS, MACHINE_PEER_VERBS, check
from tstd.protocol import ContextCompacted
from tstd.provider import ChatMessage, content_as_text
from tstd.session import Session

_HEADER = "[Earlier conversation compacted]"


def _meter() -> ContextMeter:
    return ContextMeter("brain", _tier(), HeuristicTokenCounter(), None)


def _users(messages: list[ChatMessage]) -> list[str]:
    return [content_as_text(m.content) for m in messages if m.role == "user"]


def _summaries(messages: list[ChatMessage]) -> list[str]:
    texts = [content_as_text(m.content) for m in messages if m.role == "system"]
    return [t for t in texts if t.startswith(_HEADER)]


def _saved(daemon: Daemon, session_id: str) -> list[ChatMessage]:
    loaded = daemon._session_persist.load(session_id)
    assert loaded is not None and loaded.conversation is not None
    return loaded.conversation


# ── the verb is classified like the other session verbs ─────────────────


def test_compact_context_is_forwarded_like_fork_from() -> None:
    assert "compact_context" in MACHINE_PEER_VERBS
    assert "compact_context" in FORWARDED_VERBS
    bridged = _msg("compact_context", session_id=BRIDGED, guidance="keep tests")
    own = _msg("compact_context", session_id="s1", user_index=2)
    for viewer in (VIEWER, PHONE_VIEWER):
        assert check(bridged, viewer, bridged=True) is None
    # The hub's forward reaches the owning computer as a machine peer.
    assert check(own, MACHINE, bridged=False) is None
    # Bridging stays one hop.
    second_hop = check(bridged, MACHINE, bridged=True)
    assert second_hop is not None and json.loads(second_hop)["code"] == "bridged_session"


# ── through a real hub ───────────────────────────────────────────────────


async def test_a_hub_compacts_a_session_on_another_computer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeKeychain().install(monkeypatch)
    fleet = Fleet(monkeypatch)
    try:
        work = tmp_path / "ws-b"
        b = await fleet.start(tmp_path / "b")
        a = await fleet.start(tmp_path / "a")
        async with window(b) as b_screen, window(a, VIEWER_FEATURES) as hub:
            sid = await _open(b_screen, work)
            session = b.session_registry.get(sid)
            assert session is not None
            session.conversation.append(ChatMessage(role="system", content="PROMPT"))
            for n in range(3):
                session.conversation.append(ChatMessage(role="user", content=f"question {n}"))
                session.conversation.append(ChatMessage(role="assistant", content=f"answer {n}"))
            session.context_meter = _meter()

            mch = await _pair(hub, b)
            remote = f"{sid}:{mch}"
            await hub.send(json.dumps({"type": "attach", "session_id": remote, "from_seq": 1}))
            await _until(hub, _for(remote, "tier_state"))  # the replay is in
            await hub.send(
                json.dumps(
                    {"type": "compact_context", "session_id": remote, "guidance": "keep tests"}
                )
            )
            frame = await _until(hub, _for(remote, "context_compacted"))
            assert frame["type"] == "context_compacted", frame
            assert (frame["trigger"], frame["guidance"]) == ("manual", "keep tests")

            assert _users(session.conversation) == ["question 2"]
            # Saved on the computer that owns it, not only in its memory.
            saved = _saved(b, sid)
            assert _users(saved) == ["question 2"]
            assert len(_summaries(saved)) == 1
    finally:
        await fleet.close()


# ── a compaction is saved before anything else happens ─────────────────


async def test_compaction_on_request_survives_a_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    mock = MockProvider(default=Script(kind="stream", content="ok"))
    daemon = Daemon(data_dir=data_dir, provider=mock)
    try:
        await daemon._start_session(str(workspace))
        (session,) = await daemon.session_registry.list_sessions()
        for n in range(3):
            await session.add_user_message(f"question {n}")
            await wait_for_turn(session, n + 1, 15.0)
        # The loop saves after each turn; wait for the last one to land so
        # only the compaction can change the file from here on.
        await eventually(lambda: len(_users(_saved(daemon, session.id))) == 3)
        if session.context_meter is None:
            session.context_meter = _meter()

        reply = await daemon._handle_message(
            json.dumps({"type": "compact_context", "session_id": session.id, "user_index": 1}),
            None,
        )
        assert reply is None, reply

        def compacted() -> list[ContextCompacted]:
            return [e for e in session.event_log.all_events if isinstance(e, ContextCompacted)]

        await eventually(lambda: bool(compacted()))
        assert [e.trigger for e in compacted()] == ["from_here"]
        assert _users(session.conversation) == ["question 0"]
        assert _users(_saved(daemon, session.id)) == ["question 0"]
        session_id = session.id
    finally:
        await _stop_runners(daemon)

    revived = Daemon(data_dir=data_dir, provider=MockProvider(default=Script(kind="stream")))
    try:
        await revived._restore_sessions()
        again = revived.session_registry.get(session_id)
        assert again is not None
        assert _users(again.conversation) == ["question 0"]
        (summary,) = _summaries(again.conversation)
        assert "(2 user turns)" in summary
        # The window's numbering still maps through the saved summary.
        again.context_meter = _meter()
        assert await again.compact_now(user_index=2) == "user_turn_compacted"
    finally:
        await _stop_runners(revived)


async def test_compact_now_saves_through_the_persist_hook(tmp_path: Path) -> None:
    """The narrow contract: a successful compaction calls the hook once, a
    refused one never does."""
    session = Session(str(tmp_path))
    saved: list[list[ChatMessage]] = []

    async def snap() -> None:
        saved.append(list(session.conversation))

    session._conversation_hook = snap
    session.context_meter = _meter()
    session.conversation.append(ChatMessage(role="system", content="PROMPT"))
    assert await session.compact_now() == "nothing_to_compact"
    for n in range(3):
        session.conversation.append(ChatMessage(role="user", content=f"question {n}"))
        session.conversation.append(ChatMessage(role="assistant", content=f"answer {n}"))
    assert await session.compact_now(user_index=9) == "user_turn_not_found"
    assert saved == []
    assert await session.compact_now("keep tests") is None
    assert len(saved) == 1
    assert _users(saved[0]) == ["question 2"]
