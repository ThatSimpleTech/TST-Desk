"""TD-5161: low-severity review notes from waves 7-10.

Each test pins one note that was real on main: it fails on the old code.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_backup_endpoints import BACKUP_TIER, _config
from tstd.cli_turn import _TurnReader
from tstd.config import load_config
from tstd.config_write import save_tier_slug
from tstd.fallback import TurnBackups
from tstd.local_worker import tier_state_event
from tstd.protocol import ApprovalRequest, Question, QuestionOption, QuestionRequest
from tstd.router import TierRouter
from tstd.scheduler.park import pending_tool_summary, waiting_line
from tstd.session import Session
from tstd.tools.consult import CONSULT_TOOL


def test_consult_tells_a_brain_its_default_is_the_validator() -> None:
    """Note 9: a brain told "brain (the default)" was steered into a refusal."""
    to = CONSULT_TOOL.parameters["properties"]["to"]["description"]
    for text in (CONSULT_TOOL.description, to):
        assert "brain (the default)" not in text and "brain (default)" not in text
        assert "validator when you are the brain" in text


_PRICES = """input_price: 0
{i}output_price: 0
{i}cache_read_price: 0
{i}context_window: 32768
{i}max_output_tokens: 1000"""


def test_a_slug_write_never_splices_into_a_backup(tmp_path: Path) -> None:
    """Note 10: a commented `slug:` inside a `fallbacks` entry was the placeholder."""
    tier = "      base_url: http://127.0.0.1:8080/v1\n      " + _PRICES.format(i="      ")
    text = f"""active_preset: p
presets:
  p:
    brain:
      fallbacks:
        - slug: vendor/backup
          # slug: vendor/older-backup
          base_url: http://127.0.0.1:8081/v1
          {_PRICES.format(i="          ")}
{tier}
    worker:
      slug: m
{tier}
    validator:
      slug: m
{tier}
"""
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    assert load_config(path).tier("brain").slug in (None, "")
    save_tier_slug("p", "brain", "new-model", path)
    brain = load_config(path).tier("brain")
    assert brain.slug == "new-model"
    assert [b.slug for b in brain.fallbacks] == ["vendor/backup"]
    assert "# slug: vendor/older-backup" in path.read_text(encoding="utf-8")


def test_a_mid_turn_tier_state_keeps_naming_the_backup() -> None:
    """Note 11: set_plan/set_tier mid-turn reset the pill to the dead endpoint."""
    config = _config(BACKUP_TIER)
    session = Session("/tmp/ws")
    session.router = TierRouter()
    backups = TurnBackups()
    chain = backups.chain_for(config, "brain", config.tier("brain"))
    chain.advance()
    session.turn_backups = backups
    session._open_turns = 1
    during = tier_state_event(session, config)
    assert during.fallbacks == {"brain": 1}
    assert during.model_slugs["brain"] == "backup-model"
    assert during.hosts["brain"] == "backup.local"
    session._open_turns = 0  # between turns the next one starts on the own model
    after = tier_state_event(session, config)
    assert after.fallbacks == {} and after.model_slugs["brain"] == "own-model"


async def test_a_run_parked_on_a_question_says_so() -> None:
    """Note 16: the channel called a question an approval, with an older card's summary."""
    session = Session("/tmp/ws")
    await session.event_log.add(
        ApprovalRequest(
            session_id=session.id,
            tool_call_id="c1",
            tool_name="shell",
            arguments={},
            decision_class="B",
            summary="run `make deploy`",
            reason="",
            seq=1,
        )
    )
    options = [QuestionOption(label="Postgres"), QuestionOption(label="SQLite")]
    await session.event_log.add(
        QuestionRequest(
            session_id=session.id,
            tool_call_id="c2",
            questions=[Question(question="Which database?", options=options)],
            seq=1,
        )
    )
    summary = pending_tool_summary(session)
    assert "make deploy" not in summary
    assert waiting_line(None, None, summary) == (
        "This job is waiting for your answer to: Which database?"
    )
    assert waiting_line(None, None, "run `ls`") == "This job is waiting for your approval: run `ls`"


async def test_tst_run_returns_when_the_turn_pauses() -> None:
    """Note 19: a paused turn (the progress guard) left `tst run` waiting out its budget."""
    reader = _TurnReader(session_id="s", output="json", ours=True, content=None)
    paused = {
        "type": "session_state",
        "session_id": "s",
        "state": "paused",
        "reason": "progress guard: the same tool batch three times in a row",
    }
    outcome = await reader.feed(None, paused)
    assert outcome is not None
    assert (outcome.status, outcome.exit_code) == ("approval_needed", 4)
    assert "progress guard" in (outcome.error_message or "")
    running = {"type": "session_state", "session_id": "s", "state": "running"}
    assert (
        await _TurnReader(session_id="s", output="json", ours=True, content=None).feed(
            None, running
        )
        is None
    )
