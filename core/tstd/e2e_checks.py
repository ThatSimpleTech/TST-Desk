"""What one harness pass proves — the verdict half (TD-1401, TD-1803, TD-1807).

``e2e_harness`` drives a session and collects the events it produced; this
module decides what those events prove.  The split is by responsibility:
driving a protocol client and judging a transcript are different jobs, and
TD-1807 grew only the second one.

**Selection is by identity, never by position.**  The call under test is the
first ``fs_write`` the model made; its approval and its result are then
matched on that call's ``tool_call_id``, so all three checks are talking
about the same call.  A model that reads a file before it writes one is
behaving legitimately, and used to fail here because ``calls[0]`` was the
read — measured at one failure in five consecutive live runs.

**Extra tool calls are reported, not judged.**  They neither fail the pass
nor slip by unmentioned: they land in :attr:`HarnessResult.notes`, printed
under the checks.  The pass proves the required write went through the whole
chain, not that the model was economical about getting there.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import AuditStore
from .audit_queries import cost_by_session
from .e2e_plan import HarnessPlan, RecordingProvider

# The tool call the pass is about, and the file it must leave behind.
# Both plans script the same task, so this is a fact about the harness
# rather than something a plan varies.
_REQUIRED_TOOL = "fs_write"
_WRITTEN_FILE = "hello.txt"


@dataclass
class HarnessResult:
    """Outcome of one harness pass: named checks with a detail line each.

    ``notes`` carry what the model did that no check judges — extra tool
    calls, above all.  They never move :attr:`ok`; they exist so a pass
    cannot go green while quietly hiding behaviour worth seeing.
    """

    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)

    def report(self) -> str:
        lines = [
            f"{'PASS' if p else 'FAIL'}  {name:<28} {detail}" for name, p, detail in self.checks
        ]
        lines += [f"NOTE  {note}" for note in self.notes]
        verdict = "OVERALL PASS" if self.ok else "OVERALL FAIL"
        lines.append(f"{verdict} in {self.elapsed:.1f}s")
        return "\n".join(lines)


def _check(result: HarnessResult, name: str, ok: bool, detail: str = "") -> None:
    result.checks.append((name, ok, detail))


def _group(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_type.setdefault(str(event.get("type", "")), []).append(event)
    return by_type


def _call_id(call: dict[str, Any]) -> str:
    return str(call.get("tool_call_id", ""))


def _names(calls: list[dict[str, Any]]) -> str:
    return ", ".join(str(c.get("name")) for c in calls) or "no tools at all"


def _describe(call: dict[str, Any], results: list[dict[str, Any]]) -> str:
    """One tool call as ``name#id → status``, for a report line."""
    status = next(
        (r.get("status") for r in results if r.get("tool_call_id") == _call_id(call)),
        "no result",
    )
    return f"{call.get('name')}#{_call_id(call)} → {status}"


def _select_write(
    calls: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The call under test, plus everything else the model asked for.

    The *first* matching call, not whichever one happens to have succeeded:
    hunting for the call that makes the pass green would turn the checks
    into a search for agreement with themselves (AGENTS.md §7).
    """
    chosen = next((c for c in calls if c.get("name") == _REQUIRED_TOOL), None)
    others = [c for c in calls if c is not chosen]
    return chosen, others


def _check_workspace(result: HarnessResult, by_type: dict[str, list[dict[str, Any]]]) -> None:
    """1. Workspace open — session running, boundary resolved."""
    state = by_type.get("session_state", [{}])[0]
    _check(
        result,
        "workspace open",
        state.get("state") in ("running", "idle"),
        f"state={state.get('state')!r}",
    )
    boundary = by_type.get("boundary_update", [{}])[0]
    _check(
        result,
        "boundary resolved",
        bool(boundary.get("writable_paths")),
        f"source={boundary.get('source')!r}",
    )


def _check_steering(result: HarnessResult, provider: RecordingProvider) -> None:
    """2. The workspace ``AGENTS.md`` must reach the model's system prompt.

    Read off the recorded request, not the wire: ``steering_reloaded`` is a
    hot-reload event (TD-509) and never fires on a fresh session's first
    turn, so no event carries this.
    """
    first_system = ""
    if provider.calls:
        first = provider.calls[0]
        if first.messages and first.messages[0].role == "system":
            first_system = first.messages[0].content or ""
    _check(
        result,
        "steering resolved",
        "Harness workspace" in first_system,
        "workspace AGENTS.md in system prompt" if first_system else "no provider calls recorded",
    )


def _check_tool_call(
    result: HarnessResult, by_type: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """3+4. The required call was made, and carries a classification.

    Returns the selected call so the approval and execution checks can be
    about the same one.  ``{}`` when the model never made it — which fails
    here and, through the empty id, fails those two as well.
    """
    calls = by_type.get("tool_call", [])
    chosen, others = _select_write(calls)
    if others:
        described = ", ".join(_describe(c, by_type.get("tool_result", [])) for c in others)
        result.notes.append(f"extra tool calls (do not affect the verdict): {described}")

    if chosen is None:
        _check(
            result,
            "tool call classified",
            False,
            f"no {_REQUIRED_TOOL} call; model called {_names(calls)}",
        )
        return {}

    # Say where in the transcript the checked call sits, rather than
    # claiming an order the harness has not established.
    position = next(i for i, c in enumerate(calls) if c is chosen) + 1
    among = f" (call {position} of {len(calls)})" if others else ""
    _check(
        result,
        "tool call classified",
        chosen.get("decision_class") in ("A", "B"),
        f"{_REQUIRED_TOOL}#{_call_id(chosen)} class={chosen.get('decision_class')!r}{among}",
    )
    return chosen


def _check_approval(
    result: HarnessResult,
    by_type: dict[str, list[dict[str, Any]]],
    plan: HarnessPlan,
    call: dict[str, Any],
) -> None:
    """4.5. The gate that opened for *this* call, when the plan expects one.

    The mock plan's scripted write classifies A and runs automatically, so
    requiring a gate there would fail a pass that is behaving correctly —
    this check exists precisely where ``approve_on`` says a pending
    approval is registered.
    """
    if plan.approve_on != "approval_request":
        return
    gate = by_type.get("approval_request", [])
    call_id = _call_id(call)
    matched = [g for g in gate if g.get("tool_call_id") == call_id] if call_id else []
    if matched:
        _check(result, "approval gate", True, f"reason={matched[0].get('reason')!r}")
        return
    opened = ", ".join(f"{g.get('tool_name')}#{g.get('tool_call_id')}" for g in gate) or "none"
    _check(
        result,
        "approval gate",
        False,
        f"no approval_request for the {_REQUIRED_TOOL} call; gates opened: {opened}",
    )


def _check_execution(
    result: HarnessResult,
    by_type: dict[str, list[dict[str, Any]]],
    plan: HarnessPlan,
    workspace: Path,
    call: dict[str, Any],
) -> None:
    """5+6. The selected call's own result, and the file it should have left."""
    call_id = _call_id(call)
    results = by_type.get("tool_result", [])
    own = next((r for r in results if r.get("tool_call_id") == call_id), {}) if call_id else {}
    written = workspace / _WRITTEN_FILE
    wrote_file = written.exists() and plan.content_ok(written.read_text(encoding="utf-8"))
    status = own.get("status") if own else f"no result for {_REQUIRED_TOOL}#{call_id or '?'}"
    _check(
        result,
        "execution",
        own.get("status") == "success" and wrote_file,
        f"status={status!r} file={'written' if wrote_file else 'missing'}",
    )


async def _check_checkpoint(result: HarnessResult, workspace: Path, session_id: str) -> None:
    """7. Checkpoint — the mutation is committed on the session branch."""
    branch = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(workspace), "log", "--oneline", f"tst/session/{session_id}"],
        capture_output=True,
        text=True,
    )
    _check(
        result,
        "checkpoint",
        branch.returncode == 0 and bool(branch.stdout.strip()),
        f"branch=tst/session/{session_id}",
    )


def _check_accounting(
    result: HarnessResult,
    by_type: dict[str, list[dict[str, Any]]],
    plan: HarnessPlan,
    data_dir: Path,
    session_id: str,
) -> None:
    """8+9. Audit rows carry real token counts, and the turn billed what
    the plan's preset says it should."""
    aggregates = cost_by_session(AuditStore(data_dir / "audit.db"))
    _check(
        result,
        "ledger",
        any(a.key == session_id and a.prompt_tokens > 0 for a in aggregates),
        f"sessions={len(aggregates)}",
    )
    turn = by_type.get("turn_complete", [{}])[-1]
    cost = float(turn.get("cost", 0.0))
    # A zero-price preset bills nothing, and nothing is the honest answer.
    # The ledger check above is what proves free is still *tracked*.
    billed = cost > 0 if plan.expect_spend else cost >= 0
    _check(
        result,
        "cost accounting",
        billed and int(turn.get("tokens", 0)) > 0,
        f"cost={turn.get('cost')} tokens={turn.get('tokens')}",
    )


async def verify(
    *,
    events: list[dict[str, Any]],
    plan: HarnessPlan,
    workspace: Path,
    data_dir: Path,
    session_id: str,
    started: float,
) -> HarnessResult:
    """Judge one harness pass from the events it produced."""
    result = HarnessResult()
    by_type = _group(events)

    _check_workspace(result, by_type)
    _check_steering(result, plan.provider)
    call = _check_tool_call(result, by_type)
    _check_approval(result, by_type, plan, call)
    _check_execution(result, by_type, plan, workspace, call)
    await _check_checkpoint(result, workspace, session_id)
    _check_accounting(result, by_type, plan, data_dir, session_id)

    result.elapsed = time.monotonic() - started
    _check(
        result,
        f"under {int(plan.budget_secs)} seconds",
        result.elapsed < plan.budget_secs,
        f"{result.elapsed:.1f}s",
    )
    return result
