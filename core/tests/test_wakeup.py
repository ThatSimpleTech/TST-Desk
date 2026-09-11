"""Wake-up summary (TD-4303).

On autonomy complete / stop / breaker the session log and the notify
channel carry what changed, a ledger excerpt, refusals, and the auto
branch. Interactive sessions never emit ``autonomy_summary``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_autonomy_loop import _wire_autonomy, make_charter
from tests.test_checkpoint import _git, make_repo
from tests.test_dispatch import make_config
from tests.test_loop import start_loop as start_text_loop
from tstd.autonomy.dod import DodItemResult, DodPoll
from tstd.autonomy.ledger import DecisionLedger, LedgerEntry
from tstd.autonomy.runner import (
    CLASS_C_STOP,
    DOD_MET,
    advance_autonomy,
    notify_autonomy_stop,
    should_notify,
)
from tstd.autonomy.wakeup import (
    LEDGER_RELATIVE_PATH,
    deliver_wakeup,
    format_notify_text,
)
from tstd.mock import MockProvider, Script
from tstd.protocol import AutonomySummary
from tstd.router import TierRouter
from tstd.session import Session

SECRET = "sk-abcdefghijklmnopqrstuvwxyz1234"


def _summaries(session: Session) -> list[AutonomySummary]:
    return [e for e in session.event_log.all_events if isinstance(e, AutonomySummary)]


class TestShouldNotify:
    def test_known_stops_and_breaker_prefix(self) -> None:
        assert should_notify(CLASS_C_STOP)
        assert should_notify(DOD_MET)
        assert should_notify("iteration cap exceeded: 2 >= 2")
        assert should_notify("spend cap exceeded")
        assert should_notify("wall-clock cap exceeded")
        assert should_notify("breaker: validator drift")
        assert should_notify("breaker:")
        assert not should_notify("definition of done")
        assert not should_notify("breaker tripped")


class TestAdvanceEmits:
    async def test_stop_emits_summary_with_branch_and_ledger(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        notified = _wire_autonomy(session, make_charter(max_iterations=1))
        assert await advance_autonomy(session) is False
        events = _summaries(session)
        assert len(events) == 1
        ev = events[0]
        assert ev.session_id == session.id
        assert ev.branch == "tst/auto/ship-the-csv-importer"
        assert ev.ledger_path == LEDGER_RELATIVE_PATH
        assert ev.reason.startswith("iteration cap")
        assert notified
        assert "Branch: tst/auto/ship-the-csv-importer" in notified[0]
        assert f"Ledger: {LEDGER_RELATIVE_PATH}" in notified[0]
        assert "Autonomy complete: definition of done met" not in notified[0]

    async def test_dod_notify_is_richer_than_oneliner(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        notified = _wire_autonomy(session, make_charter(max_iterations=8))

        async def _green() -> DodPoll:
            return DodPoll((DodItemResult("The suite is green", True, "GREEN", "worker"),))

        session.dod_poller = _green
        assert await advance_autonomy(session) is False
        assert session.autonomy_stop_reason == DOD_MET
        assert notified
        assert notified[0] != "Autonomy complete: definition of done met"
        assert DOD_MET in notified[0]
        assert "Branch:" in notified[0]
        assert "Ledger excerpt:" in notified[0]

    async def test_breaker_reason_notifies(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        notified = _wire_autonomy(session, make_charter())
        session.autonomy_stop_reason = "breaker: validator drift"
        assert await advance_autonomy(session) is False
        events = _summaries(session)
        assert events and events[0].reason == "breaker: validator drift"
        assert "breaker: validator drift" in notified[0]


class TestInteractiveNeverEmits:
    async def test_deliver_wakeup_is_noop_without_autonomy(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.autonomy_stop_reason = DOD_MET
        await deliver_wakeup(session)
        assert _summaries(session) == []

    async def test_interactive_turn_never_emits(self, tmp_path: Path) -> None:
        from tests.test_dispatch import wait_for_turn

        session = Session(str(tmp_path))
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="ok")})
        runner = await start_text_loop(session, TierRouter(), mock, make_config())
        await session.add_user_message("hello")
        await wait_for_turn(session, 1)
        assert session.autonomy is False
        assert _summaries(session) == []
        await runner.cancel()


class TestRedaction:
    async def test_key_shaped_refusal_is_not_raw(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        notified = _wire_autonomy(session, make_charter())
        session.mark_class_c(f"blocked because {SECRET}")
        assert await advance_autonomy(session) is False
        events = _summaries(session)
        assert events
        dumped = events[0].model_dump_json()
        assert SECRET not in dumped
        assert SECRET not in notified[0]
        assert "[REDACTED]" in events[0].refusals[0]
        assert "[REDACTED]" in notified[0]


class TestChangedAndLedger:
    async def test_changed_paths_vs_default_branch(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        _git(repo, "checkout", "-b", "tst/auto/ship-the-csv-importer")
        (repo / "importer.py").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "importer.py")
        _git(repo, "commit", "-m", "work")
        session = Session(str(repo))
        _wire_autonomy(session, make_charter(max_iterations=1))
        assert await advance_autonomy(session) is False
        ev = _summaries(session)[0]
        assert "importer.py" in ev.changed

    async def test_empty_changed_when_branch_missing(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        _wire_autonomy(session, make_charter(max_iterations=1))
        assert await advance_autonomy(session) is False
        assert _summaries(session)[0].changed == []

    async def test_ledger_excerpt_is_capped_last_entries(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        ledger = DecisionLedger(repo)
        await ledger.append(
            LedgerEntry(
                decision_class="B",
                what="picked ruff",
                why="already in the repo",
                commit=None,
            )
        )
        session = Session(str(repo))
        _wire_autonomy(session, make_charter(max_iterations=1))
        assert await advance_autonomy(session) is False
        ev = _summaries(session)[0]
        assert "picked ruff" in ev.ledger_excerpt
        assert "already in the repo" in ev.ledger_excerpt


class TestNotifyChannels:
    async def test_slack_and_ntfy_receive_summary_not_oneliner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[str] = []

        async def _capture(_config: object, message: str, **_kwargs: object) -> None:
            sent.append(message)

        monkeypatch.setattr("tstd.notify.slack.send", _capture)
        monkeypatch.setattr("tstd.notify.ntfy.send", _capture)
        monkeypatch.setattr("tstd.notify.discord.send", _capture)
        monkeypatch.setattr("tstd.notify.telegram.send", _capture)

        session = Session(str(tmp_path))
        _wire_autonomy(session, make_charter(max_iterations=8))

        async def _notify(message: str) -> None:
            await notify_autonomy_stop(make_config(), message)

        session.autonomy_notify = _notify

        async def _green() -> DodPoll:
            return DodPoll((DodItemResult("The suite is green", True, "GREEN", "worker"),))

        session.dod_poller = _green
        assert await advance_autonomy(session) is False
        assert len(sent) == 4
        for body in sent:
            assert body != "Autonomy complete: definition of done met"
            assert DOD_MET in body
            assert "Branch:" in body
            assert "Ledger:" in body

    def test_format_notify_text_includes_sections(self) -> None:
        text = format_notify_text(
            AutonomySummary(
                session_id="s",
                reason=DOD_MET,
                branch="tst/auto/ship-the-csv-importer",
                ledger_path=LEDGER_RELATIVE_PATH,
                changed=["a.py"],
                refusals=["nope"],
                ledger_excerpt="**Chose:** x",
                seq=1,
            )
        )
        assert text.startswith("Autonomy complete:")
        assert "Branch: tst/auto/ship-the-csv-importer" in text
        assert "- a.py" in text
        assert "- nope" in text
        assert "**Chose:** x" in text
