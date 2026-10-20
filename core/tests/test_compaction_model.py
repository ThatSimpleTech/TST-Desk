"""TD-5128: model-written compaction that keeps recent files.

The worker writes a structured summary (goal, decisions, files touched,
open items) with a prompt the user can set; anything short of a usable
answer falls back to the extractive summary. The summary can be written
in the background before the budget is reached, and the files the folded
turns read or changed, and loaded skill bodies, are back after it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.test_compaction import pad, tiny_config, tiny_tier
from tests.test_loop import mock_factory, wait_for_turn
from tstd.compaction import budget_threshold, estimate_tokens
from tstd.compaction_files import FileAccess, recent_files
from tstd.compaction_model import (
    DEFAULT_SUMMARY_PROMPT,
    CompactionSummarizer,
    compact_auto,
    compact_manual,
    usable_summary,
)
from tstd.compaction_text import _FILES_HEADER, _SUMMARY_HEADER
from tstd.config import CompactionConfig, ModelConfig, TierConfig
from tstd.context import PromptAssembler
from tstd.context.tokens import HeuristicTokenCounter
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import ContextCompacted
from tstd.provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    FunctionCall,
    ProviderError,
    ToolCall,
    content_as_text,
)
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools.boundary import Boundary, PathGuard

COUNTER = HeuristicTokenCounter()

GOOD = (
    "## Goal\nShip the parser.\n## Decisions\nUse a hand-written lexer.\n"
    "## Files touched\nsrc/lex.py: created\n## Open items\nnone"
)


def model_config(**overrides: object) -> ModelConfig:
    config = tiny_config()
    settings = CompactionConfig(summary="model", **overrides)  # type: ignore[arg-type]
    return config.model_copy(update={"compaction": settings})


def worker_tier() -> TierConfig:
    return tiny_config().tier("worker")


def _response(text: str, finish: str = "stop") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="r",
        model="test-worker",
        message=ChatMessage(role="assistant", content=text),
        finish_reason=finish,
        usage=None,
    )


class FakeWorker:
    """Answers summary calls with *answer*; counts and keeps the requests."""

    def __init__(self, answer: str | ProviderError = GOOD, finish: str = "stop") -> None:
        self.answer = answer
        self.finish = finish
        self.requests: list[ChatCompletionRequest] = []
        self.gate: asyncio.Event | None = None

    async def __call__(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.answer, ProviderError):
            return self.answer
        return _response(self.answer, self.finish)


def _access(root: Path, guards: tuple[str, ...] = ()) -> FileAccess:
    return FileAccess(PathGuard(Boundary(workspace_root=root)), guards)


def summarizer(
    worker: FakeWorker, root: Path | None = None, **overrides: object
) -> CompactionSummarizer:
    settings = CompactionConfig(summary="model", **overrides)  # type: ignore[arg-type]
    where = root or Path("/nonexistent-td5128")
    return CompactionSummarizer(settings, worker_tier(), worker, lambda: _access(where))


def conversation(turns: int, chars: int = 800) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content="You are helpful.")]
    for n in range(turns):
        messages.append(ChatMessage(role="user", content=pad(f"question {n}", chars)))
        messages.append(ChatMessage(role="assistant", content=pad(f"answer {n}", chars)))
    return messages


def summary_text(messages: list[ChatMessage]) -> str:
    return content_as_text(messages[1].content)


# ── The worker's answer ───────────────────────────────────────────────────


class TestUsableSummary:
    def test_the_built_in_prompt_needs_its_four_headings_in_order(self) -> None:
        assert usable_summary(GOOD, custom_prompt=False) == GOOD
        assert usable_summary("## Goal\nx\n## Decisions\ny", custom_prompt=False) is None
        swapped = GOOD.replace("## Goal", "## Tmp").replace("## Open items", "## Goal")
        assert usable_summary(swapped, custom_prompt=False) is None

    def test_thinking_and_preamble_are_dropped(self) -> None:
        text = f"<think>let me see</think>Sure, here it is:\n{GOOD}"
        assert usable_summary(text, custom_prompt=False) == GOOD

    def test_a_custom_prompt_takes_any_non_empty_answer(self) -> None:
        assert usable_summary("Short note.", custom_prompt=True) == "Short note."
        assert usable_summary("  <think>x</think> ", custom_prompt=True) is None


# ── Compaction with the worker ────────────────────────────────────────────


async def test_auto_compaction_uses_the_workers_structured_summary() -> None:
    worker = FakeWorker()
    messages = conversation(3)
    compacted, stats = await compact_auto(
        messages, tiny_tier(), COUNTER, None, carry="", summarizer=summarizer(worker)
    )
    assert stats is not None
    text = summary_text(compacted)
    assert text.startswith(f"{_SUMMARY_HEADER} (1 user turn)")
    assert "## Goal\nShip the parser." in text
    assert "User: question 0" not in text  # not the extractive one
    (request,) = worker.requests
    assert request.model == "test-worker"
    assert request.messages[0].content == DEFAULT_SUMMARY_PROMPT
    assert "question 0" in content_as_text(request.messages[1].content)
    assert "question 2" not in content_as_text(request.messages[1].content)  # kept verbatim


async def test_the_users_summary_prompt_replaces_the_built_in_one() -> None:
    worker = FakeWorker(answer="Only the essentials.")
    compacted, stats = await compact_auto(
        conversation(3),
        tiny_tier(),
        COUNTER,
        None,
        carry="",
        summarizer=summarizer(worker, summary_prompt="Summarize in one line."),
    )
    assert stats is not None
    assert worker.requests[0].messages[0].content == "Summarize in one line."
    assert "Only the essentials." in summary_text(compacted)


@pytest.mark.parametrize(
    ("answer", "finish"),
    [
        (ProviderError(code="server_error", message="boom"), "stop"),
        ("I could not summarize this.", "stop"),
        (GOOD, "length"),
    ],
    ids=["worker-error", "no-headings", "cut-off"],
)
async def test_a_failed_summary_falls_back_to_the_extractive_one(
    answer: str | ProviderError, finish: str
) -> None:
    worker = FakeWorker(answer=answer, finish=finish)
    compacted, stats = await compact_auto(
        conversation(3), tiny_tier(), COUNTER, None, carry="", summarizer=summarizer(worker)
    )
    assert stats is not None and stats.dropped_messages == 2
    assert "User: question 0" in summary_text(compacted)
    assert len(worker.requests) == 1


async def test_a_slow_worker_times_out_to_the_extractive_summary() -> None:
    worker = FakeWorker()
    worker.gate = asyncio.Event()  # never set
    compacted, stats = await compact_auto(
        conversation(3),
        tiny_tier(),
        COUNTER,
        None,
        carry="",
        summarizer=summarizer(worker, summary_timeout_seconds=0.05),
    )
    assert stats is not None
    assert "User: question 0" in summary_text(compacted)


async def test_extractive_setting_never_calls_the_worker() -> None:
    worker = FakeWorker()
    quiet = CompactionSummarizer(
        CompactionConfig(summary="extractive"), worker_tier(), worker, lambda: _access(Path("/"))
    )
    compacted, stats = await compact_auto(
        conversation(3), tiny_tier(), COUNTER, None, carry="", summarizer=quiet
    )
    assert stats is not None
    assert "User: question 0" in summary_text(compacted)
    assert not quiet.prefetch(conversation(3), tiny_tier(), COUNTER, None)
    assert worker.requests == []


async def test_compact_now_passes_guidance_and_keeps_the_task_carry(tmp_path: Path) -> None:
    from tstd.compaction import ContextMeter

    worker = FakeWorker()
    session = Session(str(tmp_path))
    session.conversation.extend(conversation(3, chars=40))
    session.context_meter = ContextMeter("brain", tiny_tier(), COUNTER, None)
    session.compaction_summarizer = summarizer(worker)
    assert await session.compact_now("the lexer") is None
    text = content_as_text(session.conversation[1].content)
    assert "Kept in view, as asked: the lexer" in text
    assert "## Decisions" in text
    payload = content_as_text(worker.requests[0].messages[1].content)
    assert "keep this in view: the lexer" in payload


async def test_compact_now_drops_its_result_if_the_conversation_moved(tmp_path: Path) -> None:
    from tstd.compaction import ContextMeter

    worker = FakeWorker()
    worker.gate = asyncio.Event()
    session = Session(str(tmp_path))
    session.conversation.extend(conversation(3, chars=40))
    session.context_meter = ContextMeter("brain", tiny_tier(), COUNTER, None)
    session.compaction_summarizer = summarizer(worker)
    pending = asyncio.create_task(session.compact_now())
    for _ in range(5):
        await asyncio.sleep(0)
    assert worker.requests
    session.conversation.append(ChatMessage(role="user", content="a new turn"))
    worker.gate.set()
    assert await pending == "turn_in_progress"
    assert len(session.conversation) == 8  # untouched: nothing was folded


async def test_from_here_and_manual_compaction_without_a_summarizer_still_work() -> None:
    result = await compact_manual(
        conversation(3),
        None,
        COUNTER,
        None,
        guidance="",
        from_position=3,
        carry="",
        summarizer=None,
    )
    assert result is not None
    compacted, stats = result
    assert stats.dropped_messages == 4
    assert "User: question 1" in content_as_text(compacted[3].content)


# ── In the background ────────────────────────────────────────────────────


async def test_a_summary_written_ahead_is_used_without_a_second_call() -> None:
    worker = FakeWorker()
    helper = summarizer(worker)
    tier = tiny_tier()
    # Turn 2 is in: near the budget, but nothing old enough to fold yet.
    two = conversation(1)
    two.append(ChatMessage(role="user", content=pad("question 1", 800)))
    assert helper.prefetch(two, tier, COUNTER, None)
    assert not helper.prefetch(two, tier, COUNTER, None)  # once per span
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(worker.requests) == 1
    # Turn 3 crosses the budget and folds exactly turn 1: no new call.
    three = [*two, ChatMessage(role="assistant", content=pad("answer 1", 800))]
    three.append(ChatMessage(role="user", content=pad("question 2", 800)))
    compacted, stats = await compact_auto(three, tier, COUNTER, None, carry="", summarizer=helper)
    assert stats is not None
    assert "## Goal" in summary_text(compacted)
    assert len(worker.requests) == 1


async def test_cleared_tool_results_still_match_the_summary_written_ahead() -> None:
    worker = FakeWorker()
    helper = summarizer(worker)
    call = ToolCall(id="c1", function=FunctionCall(name="fs_read", arguments='{"path":"a.py"}'))
    early = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content=pad("question 0", 800)),
        ChatMessage(role="assistant", content="", tool_calls=[call]),
        ChatMessage(role="tool", tool_call_id="c1", content=pad("file body", 800)),
        ChatMessage(role="assistant", content="done"),
        ChatMessage(role="user", content=pad("question 1", 800)),
    ]
    assert helper.prefetch(early, tiny_tier(), COUNTER, None)
    cleared = list(early)
    cleared[3] = ChatMessage(role="tool", tool_call_id="c1", content="[cleared]")
    body = await helper.summarize(cleared[1:5])
    assert body == GOOD
    assert len(worker.requests) == 1


async def test_nothing_is_written_ahead_below_the_background_share() -> None:
    worker = FakeWorker()
    helper = summarizer(worker, background_percent=99)
    small = conversation(2, chars=40)
    assert estimate_tokens(small, COUNTER)[0] * 100 < budget_threshold(tiny_tier()) * 99
    assert not helper.prefetch(small, tiny_tier(), COUNTER, None)
    off = summarizer(worker, background_percent=0)
    assert not off.prefetch(conversation(3), tiny_tier(), COUNTER, None)
    assert worker.requests == []


# ── Recent files ─────────────────────────────────────────────────────────


def _file_turn(n: int, tool: str, path: str, result_chars: int = 2) -> list[ChatMessage]:
    call = ToolCall(
        id=f"c{n}", function=FunctionCall(name=tool, arguments=json.dumps({"path": path}))
    )
    return [
        ChatMessage(role="user", content=f"turn {n}"),
        ChatMessage(role="assistant", content="", tool_calls=[call]),
        ChatMessage(role="tool", tool_call_id=f"c{n}", content=pad("ok", result_chars)),
        ChatMessage(role="assistant", content=f"did {n}"),
    ]


def _seed(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def test_recent_files_are_newest_first_and_skip_what_the_kept_turns_name() -> None:
    dropped = [*_file_turn(1, "fs_read", "a.py"), *_file_turn(2, "fs_edit", "b.py")]
    dropped += _file_turn(3, "fs_write", "c.py") + _file_turn(4, "shell", "d.py")
    kept = _file_turn(5, "fs_read", "c.py")
    assert recent_files(dropped, kept) == ["b.py", "a.py"]


async def test_files_read_or_changed_come_back_after_compaction(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        {"src/a.py": "A = 1\n", "src/b.py": "B = 2\n", ".env": "TOKEN=x\n", "big.txt": "y" * 9000},
    )
    (tmp_path.parent / "outside.py").write_text("OUT = 1\n", encoding="utf-8")
    messages = [ChatMessage(role="system", content="sys")]
    messages += _file_turn(1, "fs_read", "big.txt", 1600)
    messages += _file_turn(2, "fs_read", "src/a.py", 1600)
    messages += _file_turn(3, "fs_read", "../outside.py", 1600)
    messages += _file_turn(4, "fs_read", ".env", 1600)
    messages += _file_turn(5, "fs_edit", "src/b.py", 1600)
    messages += [ChatMessage(role="user", content="now")]
    messages += [ChatMessage(role="assistant", content="ok")]
    messages += [ChatMessage(role="user", content="next")]
    worker = FakeWorker()
    helper = CompactionSummarizer(
        CompactionConfig(summary="model", reinject_files=5, reinject_tokens=300),
        worker_tier(),
        worker,
        lambda: _access(tmp_path, guards=(".env",)),
    )
    tier = tiny_tier(context_window=2000, max_output_tokens=50)
    compacted, stats = await compact_auto(
        messages, tier, COUNTER, None, carry="", summarizer=helper
    )
    assert stats is not None
    text = summary_text(compacted)
    files = text.split(_FILES_HEADER, 1)[1]
    # Newest first; the oldest, big.txt, is cut to what is left of the budget.
    order = [files.index(f"--- {name} ---") for name in ("src/b.py", "src/a.py", "big.txt")]
    assert order == sorted(order)
    assert "B = 2" in files and "A = 1" in files
    assert "cut to fit" in files
    assert "outside.py" not in files and "OUT = 1" not in files
    assert "TOKEN=x" not in files  # a read rule asks for it
    assert COUNTER.count(files).count <= 300 + 10
    assert stats.tokens_after <= budget_threshold(tier)


async def test_file_budget_counts_files_and_stays_under_half_the_headroom(tmp_path: Path) -> None:
    _seed(tmp_path, {f"f{n}.py": f"V{n} = {n}\n" for n in range(4)})
    messages = [ChatMessage(role="system", content="sys")]
    for n in range(4):
        messages += _file_turn(n, "fs_read", f"f{n}.py", 1600)
    messages += [ChatMessage(role="user", content="now")]
    messages += [ChatMessage(role="assistant", content="ok")]
    messages += [ChatMessage(role="user", content="next")]
    helper = CompactionSummarizer(
        CompactionConfig(summary="model", reinject_files=2),
        worker_tier(),
        FakeWorker(),
        lambda: _access(tmp_path),
    )
    tier = tiny_tier(context_window=1000, max_output_tokens=50)
    compacted, _ = await compact_auto(messages, tier, COUNTER, None, carry="", summarizer=helper)
    files = summary_text(compacted).split(_FILES_HEADER, 1)[1]
    assert "V3 = 3" in files and "V2 = 2" in files
    assert "V1 = 1" not in files and "V0 = 0" not in files


async def test_a_later_compaction_does_not_repeat_old_files(tmp_path: Path) -> None:
    _seed(tmp_path, {"a.py": "A = 1\n"})
    worker = FakeWorker()
    helper = CompactionSummarizer(
        CompactionConfig(summary="model"), worker_tier(), worker, lambda: _access(tmp_path)
    )
    messages = [ChatMessage(role="system", content="sys"), *_file_turn(1, "fs_read", "a.py", 1600)]
    messages += [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="a"),
    ]
    messages += [ChatMessage(role="user", content="r")]
    tier = tiny_tier()
    first, _ = await compact_auto(messages, tier, COUNTER, None, carry="", summarizer=helper)
    assert "A = 1" in summary_text(first)
    second_input = [*first, ChatMessage(role="assistant", content="b")]
    second_input += [ChatMessage(role="user", content=pad("s", 1800))]
    await compact_auto(second_input, tier, COUNTER, None, carry="", summarizer=helper)
    payload = content_as_text(worker.requests[-1].messages[1].content)
    assert "A = 1" not in payload  # the old file copy is not summarized again


# ── Through the agent loop ───────────────────────────────────────────────


def _is_summary_call(call: ChatCompletionRequest) -> bool:
    return content_as_text(call.messages[0].content) == DEFAULT_SUMMARY_PROMPT


def _seed_workspace(workspace: Path, skill_body: str = "") -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text("Test workspace steering.\n")
    if skill_body:
        skill = workspace / ".tst" / "skills" / "pack"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\ndescription: a pack\n---\n{skill_body}\n", encoding="utf-8"
        )


async def _run(
    tmp_path: Path, config: ModelConfig, worker: Script, skill_body: str = ""
) -> tuple[Session, MockProvider, SessionRunner]:
    workspace = tmp_path / "ws"
    _seed_workspace(workspace, skill_body)
    session = Session(str(workspace))
    if skill_body:
        session.loaded_skills.append("pack")
    mock = MockProvider(
        scripts={
            "test-brain": Script(kind="stream", content=pad("reply", 800)),
            "test-worker": worker,
        }
    )
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(lead_turns=10),  # every turn on the brain
            mock_factory(mock),
            config,
            prompt_assembler=PromptAssembler(workspace, home_dir=tmp_path / "home"),
        ),
    )
    await runner.start()
    for i in range(1, 4):
        await session.add_user_message(pad(f"turn{i}", 800))
        await wait_for_turn(session, i)
    return session, mock, runner


async def test_the_loop_compacts_with_a_summary_written_during_the_turn_before(
    tmp_path: Path,
) -> None:
    session, mock, runner = await _run(
        tmp_path, model_config(), Script(kind="stream", content=GOOD)
    )
    events = [e for e in session.event_log.all_events if isinstance(e, ContextCompacted)]
    assert len(events) == 1
    turns = [i for i, call in enumerate(mock.calls) if call.model == "test-brain"]
    summaries = [i for i, call in enumerate(mock.calls) if _is_summary_call(call)]
    assert len(turns) == 3
    # Turn 3 folded turn 1 with the summary written while turn 2 ran: one
    # call, made before turn 3's request (later ones are for the next fold).
    assert [i for i in summaries if i < turns[2]] == [summaries[0]]
    assert turns[1] < summaries[0] < turns[2]
    sent = mock.calls[turns[2]].messages
    assert sent[1].role == "system"
    assert "## Goal\nShip the parser." in content_as_text(sent[1].content)
    await runner.cancel()


async def test_the_loop_falls_back_when_the_worker_fails(tmp_path: Path) -> None:
    _session, mock, runner = await _run(
        tmp_path,
        model_config(background_percent=0),
        Script(kind="error", status_code=500, content="down"),
    )
    sent = [c for c in mock.calls if c.model == "test-brain"][-1].messages
    text = content_as_text(sent[1].content)
    assert text.startswith(_SUMMARY_HEADER)
    assert "User: turn1" in text
    await runner.cancel()


async def test_a_loaded_skill_body_is_in_the_prompt_after_compaction(tmp_path: Path) -> None:
    session, mock, runner = await _run(
        tmp_path,
        model_config(),
        Script(kind="stream", content=GOOD),
        skill_body="SKILL-BODY-MARKER: always run the linter.",
    )
    events = [e for e in session.event_log.all_events if isinstance(e, ContextCompacted)]
    assert events
    sent = [c for c in mock.calls if c.model == "test-brain"][-1].messages
    assert "SKILL-BODY-MARKER" in content_as_text(sent[0].content)
    await runner.cancel()
