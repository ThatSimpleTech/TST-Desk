"""The harness judges the call it means to judge (TD-1807).

The live leg failed one run in five because a local model read a file before
it wrote one: the checks took ``calls[0]``, matched the approval against
``gate[0]``, and inspected ``results[0]``.  Every one of those is a guess
about ordering that the OpenAI tool-call contract never made.

These runs are offline — a scripted provider replaying chunk sequences,
which is the only way to pin a *multi-call* ordering with no model running
(``MockProvider`` emits one tool call per script and reuses a single call
id, so it cannot express the transcripts this story is about).  The daemon,
the classifier, the policy gate and the dispatcher are all real; only the
model is scripted.

Two of the five runs must FAIL, and that is the point: a selection rule
that made every transcript pass would have replaced one broken check with
a check that proves nothing (AGENTS.md §7).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from tests.test_usage_recording import ScriptedProvider
from tstd.e2e_checks import HarnessResult
from tstd.e2e_harness import run
from tstd.e2e_plan import HarnessPlan
from tstd.policy import PolicyConfig, PolicyRule
from tstd.provider import Delta, DeltaToolCall, StreamChunk, Usage

_STEERING = "# Harness workspace\n\nKeep the greeting in hello.txt short.\n"
_CONTENT = "hello from M1\n"
# A read matches no static rule, so it classifies B and parks on the gate.
# The runs that are about *ordering* let it through, so that the only
# approval in the transcript is the one the run is actually about.
_READS_FREELY = PolicyConfig(rules=[PolicyRule(tool="fs_read", args="**", effect="auto")])
_USAGE = Usage(
    prompt_tokens=1_200,
    cached_prompt_tokens=1_000,
    completion_tokens=480,
    total_tokens=1_680,
)


def _tool_call(name: str, arguments: dict[str, str], call_id: str) -> list[StreamChunk]:
    """One provider call asking for one tool call, under its own id."""
    return [
        StreamChunk(
            id=call_id,
            delta=Delta(
                tool_calls=[
                    DeltaToolCall(
                        index=0,
                        id=call_id,
                        function_name=name,
                        function_arguments=json.dumps(arguments),
                    )
                ]
            ),
        ),
        StreamChunk(id=call_id, delta=Delta(), finish_reason="tool_calls", usage=_USAGE),
    ]


def _text(content: str) -> list[StreamChunk]:
    """The provider call that closes the turn."""
    return [
        StreamChunk(id="close", delta=Delta(content=content)),
        StreamChunk(id="close", delta=Delta(), finish_reason="stop", usage=_USAGE),
    ]


def _read(path: Path, call_id: str) -> list[StreamChunk]:
    return _tool_call("fs_read", {"path": str(path)}, call_id)


def _write(workspace: Path, call_id: str) -> list[StreamChunk]:
    return _tool_call(
        "fs_write",
        {"path": str(workspace / "hello.txt"), "content": _CONTENT},
        call_id,
    )


def _plan(
    *calls: Sequence[StreamChunk],
    approve_on: str = "tool_call",
    policy: PolicyConfig | None = None,
) -> HarnessPlan:
    """A mock-shaped plan whose model is the given chunk script.

    ``expect_spend=False`` on purpose: what these runs pin is which call the
    checks select, and requiring a dollar figure would tie them to whichever
    preset the developer has active.  The ledger check still demands real
    token counts, so accounting is not switched off.
    """
    return HarnessPlan(
        provider=ScriptedProvider(*calls),
        steering=_STEERING,
        prompt="write the greeting",
        approve_on=approve_on,
        content_ok=lambda text: text == _CONTENT,
        expect_spend=False,
        turn_timeout=45.0,
        budget_secs=60.0,
        policy=policy,
    )


def _detail(result: HarnessResult, name: str) -> tuple[bool, str]:
    """The named check's outcome and detail line."""
    for check_name, passed, detail in result.checks:
        if check_name == name:
            return passed, detail
    raise AssertionError(f"no {name!r} check in:\n{result.report()}")


async def test_read_before_write_still_passes(tmp_path: Path) -> None:
    """The measured flake: a legitimate read ahead of the required write."""
    workspace = tmp_path / "workspace"
    result = await run(
        workspace,
        tmp_path / "data",
        _plan(
            _read(workspace / "AGENTS.md", "call-read"),
            _write(workspace, "call-write"),
            _text("Wrote hello.txt as requested."),
            policy=_READS_FREELY,
        ),
    )

    assert result.ok, f"a read before the write must not fail the pass:\n{result.report()}"
    _, detail = _detail(result, "tool call classified")
    assert "call-write" in detail, detail
    # Reported, not judged: the pass says what the model actually did.
    assert any("fs_read#call-read" in note for note in result.notes), result.report()


async def test_execution_inspects_the_write_not_the_first_result(tmp_path: Path) -> None:
    """A failed extra call does not stand in for the write's own result.

    The read is refused at the boundary, so ``results[0]`` carries
    ``status='error'`` while the write that follows succeeds.  The old
    positional check failed this run; the write went through end to end.
    """
    workspace = tmp_path / "workspace"
    result = await run(
        workspace,
        tmp_path / "data",
        _plan(
            _read(Path("/etc/hosts"), "call-outside"),
            _write(workspace, "call-write"),
            _text("Wrote hello.txt after a refused read."),
        ),
    )

    assert result.ok, f"the write's own result is what execution checks:\n{result.report()}"
    _, detail = _detail(result, "execution")
    assert "status='success'" in detail, detail
    assert any("→ error" in note for note in result.notes), result.report()


async def test_approval_is_matched_to_the_write(tmp_path: Path) -> None:
    """Two gates open; the write's approval is the one that counts."""
    workspace = tmp_path / "workspace"
    result = await run(
        workspace,
        tmp_path / "data",
        _plan(
            _read(workspace / "AGENTS.md", "call-read"),
            _write(workspace, "call-write"),
            _text("Wrote hello.txt as requested."),
            approve_on="approval_request",
            policy=PolicyConfig(rules=[PolicyRule(tool="fs_*", args="**", effect="ask")]),
        ),
    )

    assert result.ok, f"the first approval belongs to the read, not the write:\n{result.report()}"
    assert any("fs_read#call-read" in note for note in result.notes), result.report()


async def test_another_calls_approval_does_not_count_as_the_writes(tmp_path: Path) -> None:
    """A gate that opened for a different call must not bless this one.

    Only ``fs_read`` is gated here, so the write runs unapproved while an
    ``approval_request`` still exists in the transcript.  Matching by
    position would have called that the write's approval and passed.
    """
    workspace = tmp_path / "workspace"
    result = await run(
        workspace,
        tmp_path / "data",
        _plan(
            _read(workspace / "AGENTS.md", "call-read"),
            _write(workspace, "call-write"),
            _text("Wrote hello.txt as requested."),
            approve_on="approval_request",
            policy=PolicyConfig(rules=[PolicyRule(tool="fs_read", args="**", effect="ask")]),
        ),
    )

    passed, detail = _detail(result, "approval gate")
    assert not passed, f"an unrelated gate must not satisfy the write's:\n{result.report()}"
    assert "fs_read#call-read" in detail, detail
    assert not result.ok


async def test_missing_write_fails_and_names_what_happened(tmp_path: Path) -> None:
    """No write, no pass — and the report says what the model did instead."""
    workspace = tmp_path / "workspace"
    result = await run(
        workspace,
        tmp_path / "data",
        _plan(
            _read(workspace / "AGENTS.md", "call-read"),
            _text("I had a look around instead."),
            policy=_READS_FREELY,
        ),
    )

    passed, detail = _detail(result, "tool call classified")
    assert not passed, f"a run with no fs_write must fail:\n{result.report()}"
    assert "no fs_write call" in detail and "fs_read" in detail, detail
    assert not result.ok
