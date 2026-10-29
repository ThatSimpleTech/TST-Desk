"""The system message holds for a whole turn (TD-5194).

A local OpenAI-compatible server with a prompt cache reuses a request up
to its first byte that differs from the last one. The system message
comes first, so when the workspace map in it gained the file a tool had
just written, every later call of the turn re-read the whole
conversation. These tests pin the fix: within a turn each request is the
previous one with the new messages appended, and the next turn picks up
what changed on disk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.test_loop import _fs_dispatcher, make_config, mock_factory, wait_for_turn
from tstd.context import PromptAssembler, TurnPrompts
from tstd.context.assembler import newly_matched_rules
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.provider import ChatCompletionRequest, content_as_text
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "AGENTS.md").write_text("root rules\n", encoding="utf-8")
    (ws / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    # Relative tool paths resolve against the process CWD (see test_loop).
    monkeypatch.chdir(ws)
    # A local model's call outlasts the map's debounce; with it on, these
    # quick calls would all share one map whatever the loop did.
    monkeypatch.setattr("tstd.context.manifest._DEBOUNCE_SECONDS", 0)
    return tmp_path / "home", ws


async def _start(session: Session, mock: MockProvider, ws: Path, home: Path) -> SessionRunner:
    registry, dispatcher = _fs_dispatcher(ws)
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(lead_turns=50),
            mock_factory(mock),
            make_config(),
            tool_registry=registry,
            tool_dispatcher=dispatcher,
            prompt_assembler=PromptAssembler(ws, home_dir=home),
        ),
    )
    await runner.start()
    return runner


def _write_call(path: str, content: str) -> Script:
    return Script(
        kind="tool_call",
        tool_name="fs_write",
        tool_arguments=json.dumps({"path": path, "content": content}),
    )


def _turn_calls(mock: MockProvider) -> list[ChatCompletionRequest]:
    """The conversation's calls; the after-turn review (validator) is its own."""
    return [call for call in mock.calls if call.model != "test-validator"]


def _system(request: ChatCompletionRequest) -> str:
    first = request.messages[0]
    assert first.role == "system"
    return content_as_text(first.content)


def _wire(request: ChatCompletionRequest) -> dict[str, Any]:
    """The request body as the server receives it."""
    return json.loads(json.dumps(request.to_dict()))


def _assert_appended(earlier: ChatCompletionRequest, later: ChatCompletionRequest) -> None:
    """*later* is *earlier* with messages appended, byte for byte."""
    before, after = _wire(earlier), _wire(later)
    sent = before["messages"]
    assert len(after["messages"]) > len(sent)
    for index, message in enumerate(sent):
        assert json.dumps(after["messages"][index]) == json.dumps(message), index
    assert json.dumps(after.get("tools")) == json.dumps(before.get("tools"))


class TestSystemMessageHoldsForTheTurn:
    async def test_file_written_mid_turn_waits_for_the_next_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home, ws = _workspace(tmp_path, monkeypatch)
        session = Session(str(ws))
        mock = MockProvider(
            sequences={
                "test-brain": [
                    _write_call("src/icons.tsx", "export const Icon = 1;\n"),
                    _write_call("src/menu.tsx", "export const Menu = 1;\n"),
                    Script(kind="stream", content="done"),
                    Script(kind="stream", content="second turn"),
                ]
            }
        )
        runner = await _start(session, mock, ws, home)
        await session.add_user_message("add the icons")
        await wait_for_turn(session, 1)

        assert (ws / "src" / "icons.tsx").exists()
        assert (ws / "src" / "menu.tsx").exists()
        first_turn = _turn_calls(mock)
        assert len(first_turn) == 3
        # The turn's map was read before the writes, and every call of the
        # turn sends that same system message.
        assert "src/app.py" in _system(first_turn[0])
        assert "icons.tsx" not in _system(first_turn[0])
        assert _system(first_turn[1]) == _system(first_turn[0])
        assert _system(first_turn[2]) == _system(first_turn[0])
        # Nothing else in the request moves either: each call is the last
        # one with the new step appended, so a prompt cache keeps all of it.
        _assert_appended(first_turn[0], first_turn[1])
        _assert_appended(first_turn[1], first_turn[2])

        await session.add_user_message("thanks")
        await wait_for_turn(session, 2)
        second_turn = _turn_calls(mock)[3]
        assert "icons.tsx" in _system(second_turn)
        assert "menu.tsx" in _system(second_turn)
        await runner.cancel()

    async def test_next_turn_reads_the_map_even_inside_the_debounce(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The map the next turn keeps is read at its start, not a cached one."""
        home, ws = _workspace(tmp_path, monkeypatch)
        monkeypatch.setattr("tstd.context.manifest._DEBOUNCE_SECONDS", 3600)
        session = Session(str(ws))
        mock = MockProvider(
            sequences={
                "test-brain": [
                    _write_call("src/icons.tsx", "export const Icon = 1;\n"),
                    Script(kind="stream", content="done"),
                    Script(kind="stream", content="second turn"),
                ]
            }
        )
        runner = await _start(session, mock, ws, home)
        await session.add_user_message("add the icons")
        await wait_for_turn(session, 1)
        await session.add_user_message("thanks")
        await wait_for_turn(session, 2)

        calls = _turn_calls(mock)
        assert "icons.tsx" not in _system(calls[1])
        assert "icons.tsx" in _system(calls[2])
        await runner.cancel()

    async def test_memory_is_read_once_per_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Memory (and its embedding lookup) is chosen at the turn's start."""
        home, ws = _workspace(tmp_path, monkeypatch)

        @dataclass(frozen=True)
        class _Loaded:
            block: str

        loads: list[str] = []

        async def _fake_load(*_args: object, **_kwargs: object) -> _Loaded:
            loads.append("load")
            return _Loaded(block=f"<memory read {len(loads)}>")

        monkeypatch.setattr("tstd.loop.load_memory_for_turn", _fake_load)
        session = Session(str(ws))
        mock = MockProvider(
            sequences={
                "test-brain": [
                    _write_call("notes.md", "one\n"),
                    Script(kind="stream", content="done"),
                    Script(kind="stream", content="again"),
                ]
            }
        )
        runner = await _start(session, mock, ws, home)
        await session.add_user_message("write a note")
        await wait_for_turn(session, 1)
        assert len(loads) == 1
        calls = _turn_calls(mock)
        assert "<memory read 1>" in _system(calls[1])
        assert _system(calls[1]) == _system(calls[0])

        await session.add_user_message("next")
        await wait_for_turn(session, 2)
        assert len(loads) == 2
        assert "<memory read 2>" in _system(_turn_calls(mock)[2])
        await runner.cancel()

    async def test_a_skill_loaded_mid_turn_reaches_the_next_call_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``load_skill`` is the one tool whose result lives in the prompt."""
        home, ws = _workspace(tmp_path, monkeypatch)
        skill = ws / ".tst" / "skills" / "pack"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\ndescription: A pack\nwhenToUse: when asked\n---\nPACK_SKILL_BODY\n",
            encoding="utf-8",
        )
        session = Session(str(ws))
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="load_skill",
                        tool_arguments=json.dumps({"name": "pack"}),
                    ),
                    _write_call("a.txt", "a\n"),
                    Script(kind="stream", content="done"),
                ]
            }
        )
        runner = await _start(session, mock, ws, home)
        await session.add_user_message("use the pack")
        await wait_for_turn(session, 1)

        calls = _turn_calls(mock)
        assert len(calls) == 3
        assert "PACK_SKILL_BODY" not in _system(calls[0])
        assert "PACK_SKILL_BODY" in _system(calls[1])
        # Rebuilt once for the skill, then held again.
        _assert_appended(calls[1], calls[2])
        await runner.cancel()

    async def test_each_tier_keeps_its_own_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A turn that moves to the worker keeps the worker's prompt too."""
        home, ws = _workspace(tmp_path, monkeypatch)
        session = Session(str(ws))
        mock = MockProvider(
            sequences={
                "test-brain": [_write_call("a.txt", "a\n")],
                "test-worker": [
                    _write_call("b.txt", "b\n"),
                    _write_call("c.txt", "c\n"),
                    Script(kind="stream", content="done"),
                ],
            }
        )
        registry, dispatcher = _fs_dispatcher(ws)
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s,
                TierRouter(lead_turns=1),
                mock_factory(mock),
                make_config(),
                tool_registry=registry,
                tool_dispatcher=dispatcher,
                prompt_assembler=PromptAssembler(ws, home_dir=home),
            ),
        )
        await runner.start()
        await session.add_user_message("go")
        await wait_for_turn(session, 1)

        calls = _turn_calls(mock)
        assert [call.model for call in calls] == [
            "test-brain",
            "test-worker",
            "test-worker",
            "test-worker",
        ]
        assert len({_system(call) for call in calls[1:]}) == 1
        _assert_appended(calls[1], calls[2])
        _assert_appended(calls[2], calls[3])
        await runner.cancel()


class TestTurnPrompts:
    def _assembled(self, ws: Path, home: Path, touched: set[str] | None = None) -> Any:
        return PromptAssembler(ws, home_dir=home).assemble_sync("brain", matched_paths=touched)

    def _scoped(self, tmp_path: Path) -> tuple[Path, Path]:
        ws = tmp_path / "ws"
        (ws / ".tst" / "rules").mkdir(parents=True)
        (ws / ".tst" / "rules" / "src-rules.md").write_text(
            "---\nappliesTo:\n  - src/**\n---\nSRC RULES APPLY\n", encoding="utf-8"
        )
        (ws / "AGENTS.md").write_text("root\n", encoding="utf-8")
        return tmp_path / "home", ws

    def test_nothing_kept_before_the_first_assembly(self) -> None:
        assert TurnPrompts().reuse("brain", touched_paths=set(), loaded_skills=[]) is None

    def test_kept_prompt_is_reused_while_nothing_needs_it_rebuilt(self, tmp_path: Path) -> None:
        home, ws = self._scoped(tmp_path)
        assembled = self._assembled(ws, home, set())
        prompts = TurnPrompts()
        prompts.keep("brain", assembled, memory="m", project_context=None, loaded_skills=["x"])

        kept = prompts.reuse("brain", touched_paths={"README.md"}, loaded_skills=["x"])
        assert kept is not None
        assert kept.assembled is assembled
        assert kept.memory == "m"
        # Another tier has its own slot.
        assert prompts.reuse("worker", touched_paths=set(), loaded_skills=["x"]) is None

    def test_a_loaded_skill_rebuilds_it(self, tmp_path: Path) -> None:
        home, ws = self._scoped(tmp_path)
        prompts = TurnPrompts()
        prompts.keep(
            "brain",
            self._assembled(ws, home, set()),
            memory=None,
            project_context=None,
            loaded_skills=[],
        )
        assert prompts.reuse("brain", touched_paths=set(), loaded_skills=["review"]) is None

    def test_a_newly_matched_scoped_rule_rebuilds_it(self, tmp_path: Path) -> None:
        home, ws = self._scoped(tmp_path)
        assembled = self._assembled(ws, home, set())
        assert "SRC RULES APPLY" not in assembled.text
        assert newly_matched_rules(assembled.steering, {"docs/x.md"}) == []
        matched = newly_matched_rules(assembled.steering, {"src/app.py"})
        assert [p.name for p in matched] == ["src-rules.md"]

        prompts = TurnPrompts()
        prompts.keep("brain", assembled, memory=None, project_context=None, loaded_skills=[])
        assert prompts.reuse("brain", touched_paths={"src/app.py"}, loaded_skills=[]) is None

        # Once the rule is in, touching more of its files changes nothing.
        active = self._assembled(ws, home, {"src/app.py"})
        assert "SRC RULES APPLY" in active.text
        prompts.keep("brain", active, memory=None, project_context=None, loaded_skills=[])
        assert (
            prompts.reuse("brain", touched_paths={"src/app.py", "src/b.py"}, loaded_skills=[])
            is not None
        )
