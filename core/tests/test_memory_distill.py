"""Distill on the worker (TD-2301).

A worker-tier completion turns session turns plus on-disk memory into a
typed create/replace/delete proposal. The proposal is a unified diff
against disk. The worker does not write. Cost is a worker call, not a
user turn.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_loop import make_config
from tstd.cost import CostTracker
from tstd.memory_distill import DistillTurn, distill_session
from tstd.memory_store import memory_dir
from tstd.mock import MockProvider, Script
from tstd.provider import Usage


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _script(payload: object, *, prompt_tokens: int = 80, completion_tokens: int = 40) -> Script:
    return Script(
        kind="text",
        content=json.dumps(payload),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _turns() -> tuple[DistillTurn, ...]:
    return (
        DistillTurn("user", "pin ruff and refuse bare except"),
        DistillTurn("assistant", "done. ruff is the linter."),
    )


class TestTypedProposal:
    async def test_scripted_output_becomes_a_diff_against_disk(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        _write(mem / "MEMORY.md", "old index\n")
        _write(mem / "gotchas.md", "stale\n")
        payload = {
            "changes": [
                {"action": "replace", "path": "MEMORY.md", "content": "durable: ruff\n"},
                {"action": "create", "path": "auth.md", "content": "# Auth\nrefresh weekly\n"},
                {"action": "delete", "path": "gotchas.md"},
            ]
        }
        mock = MockProvider(scripts={"test-worker": _script(payload)})
        proposal = await distill_session(ws, _turns(), mock, make_config())
        assert proposal is not None
        assert [item.action for item in proposal.changes] == [
            "replace",
            "create",
            "delete",
        ]
        assert [item.name for item in proposal.changes] == [
            "MEMORY.md",
            "auth.md",
            "gotchas.md",
        ]
        replace, create, delete = proposal.changes
        assert replace.before == "old index\n"
        assert replace.after == "durable: ruff\n"
        assert "-old index" in replace.diff
        assert "+durable: ruff" in replace.diff
        assert create.before is None
        assert create.after == "# Auth\nrefresh weekly\n"
        assert "--- /dev/null" not in create.diff
        assert "+# Auth" in create.diff
        assert delete.before == "stale\n"
        assert delete.after is None
        assert "-stale" in delete.diff
        assert mem.joinpath("MEMORY.md").read_text(encoding="utf-8") == "old index\n"
        assert mem.joinpath("gotchas.md").read_text(encoding="utf-8") == "stale\n"
        assert not mem.joinpath("auth.md").exists()

    async def test_worker_call_has_no_tools(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(memory_dir(ws) / "MEMORY.md", "x\n")
        mock = MockProvider(
            scripts={
                "test-worker": _script(
                    {"changes": [{"action": "replace", "path": "MEMORY.md", "content": "y\n"}]}
                )
            }
        )
        await distill_session(ws, _turns(), mock, make_config())
        assert len(mock.calls) == 1
        assert mock.calls[0].model == "test-worker"
        assert mock.calls[0].tools is None
        assert mock.calls[0].messages[0].role == "system"
        user = mock.calls[0].messages[1].content or ""
        assert "pin ruff" in user
        assert "old index" not in user
        assert "### .tst/memory/MEMORY.md" in user
        assert "\nx\n" in user or user.endswith("x\n")

    async def test_noop_and_escapes_are_dropped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        _write(mem / "MEMORY.md", "same\n")
        payload = {
            "changes": [
                {"action": "replace", "path": "MEMORY.md", "content": "same\n"},
                {"action": "delete", "path": "missing.md"},
                {"action": "create", "path": "../AGENTS.md", "content": "no\n"},
                {"action": "create", "path": "AGENTS.md", "content": "no\n"},
                {"action": "create", "path": "nested/topic.md", "content": "no\n"},
            ]
        }
        mock = MockProvider(scripts={"test-worker": _script(payload)})
        assert await distill_session(ws, _turns(), mock, make_config()) is None

    async def test_fenced_json_is_accepted(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(memory_dir(ws) / "MEMORY.md", "old\n")
        body = (
            "```json\n"
            + json.dumps(
                {"changes": [{"action": "replace", "path": "MEMORY.md", "content": "new\n"}]}
            )
            + "\n```"
        )
        mock = MockProvider(scripts={"test-worker": Script(kind="text", content=body)})
        proposal = await distill_session(ws, _turns(), mock, make_config())
        assert proposal is not None
        assert proposal.changes[0].after == "new\n"

    async def test_garbage_is_not_a_proposal(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mock = MockProvider(scripts={"test-worker": Script(kind="text", content="nice essay")})
        assert await distill_session(ws, _turns(), mock, make_config()) is None

    async def test_provider_error_is_not_a_proposal(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mock = MockProvider(scripts={"test-worker": Script(kind="error", content="down")})
        assert await distill_session(ws, _turns(), mock, make_config()) is None


class TestCostIsAWorkerCall:
    async def test_cost_is_worker_not_a_user_turn(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(memory_dir(ws) / "MEMORY.md", "old\n")
        config = make_config()
        tracker = CostTracker(config)
        tracker.begin_turn()
        tracker.record(
            "brain",
            Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            config.tier("brain"),
        )
        turn_before = tracker.turn_cost()
        session_before = tracker.session_cost()
        mock = MockProvider(
            scripts={
                "test-worker": _script(
                    {"changes": [{"action": "replace", "path": "MEMORY.md", "content": "new\n"}]},
                    prompt_tokens=50,
                    completion_tokens=25,
                )
            }
        )
        await distill_session(ws, _turns(), mock, config, tracker)
        assert tracker.turn_cost() == turn_before
        assert tracker.session_cost() > session_before
        assert tracker.cost_by_tier()["worker"] > 0
        assert tracker.calls[-1].tier == "worker"
        assert tracker.calls[-1].model == "test-worker"
        assert tracker.calls[-1].prompt_tokens == 50
        assert tracker.calls[-1].completion_tokens == 25
