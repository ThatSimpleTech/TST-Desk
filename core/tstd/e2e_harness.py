"""M1 end-to-end headless harness (TD-1401).

Runs one scripted session against the mock provider with no UI and checks
the full chain the milestone promises: workspace open → steering
resolution → user message → tool call → classification → approval →
execution → checkpoint → ledger → cost accounting.

The daemon runs in-process but over the real WebSocket protocol — the
harness is a protocol client, not a unit test.  A scripted
:class:`MockProvider` makes the run deterministic and offline; the whole
pass must finish well inside the sixty-second CI budget.

Approval note: the approval gate itself is E8 (TD-802).  Today the
harness sends the ``approve`` message at the right point in the
conversation and asserts the classification surface (the ``tool_call``
event's ``decision_class``).  When the gate lands, this same approve
drives it — the harness contract does not change.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from .audit import AuditStore
from .audit_queries import cost_by_session
from .config import cached_config
from .daemon import Daemon
from .mock import MockProvider, Script

_STEERING_TEXT = "# Harness workspace\n\nKeep the greeting in hello.txt short.\n"
_WRITE_CONTENT = "hello from M1\n"

# Per-check timeout: a healthy run finishes in a couple of seconds; the
# story budget is sixty seconds for the whole pass including startup.
_TURN_TIMEOUT = 45.0


@dataclass
class HarnessResult:
    """Outcome of one harness pass: named checks with a detail line each."""

    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)

    def report(self) -> str:
        lines = [
            f"{'PASS' if p else 'FAIL'}  {name:<28} {detail}" for name, p, detail in self.checks
        ]
        verdict = "OVERALL PASS" if self.ok else "OVERALL FAIL"
        lines.append(f"{verdict} in {self.elapsed:.1f}s")
        return "\n".join(lines)


def _check(result: HarnessResult, name: str, ok: bool, detail: str = "") -> None:
    result.checks.append((name, ok, detail))


def _prepare_workspace(workspace: Path) -> None:
    """A clean git baseline with one steering file (AGENTS.md)."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(_STEERING_TEXT, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "user.name=TST Desk Harness",
                "-c",
                "user.email=harness@localhost",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", "AGENTS.md")
    git("commit", "-q", "-m", "baseline")


def _mock_provider(workspace: Path) -> MockProvider:
    """Turn script: write hello.txt, then close the turn with text."""
    write_args = json.dumps({"path": str(workspace / "hello.txt"), "content": _WRITE_CONTENT})
    brain = cached_config().tier("brain").slug
    return MockProvider(
        sequences={
            brain: [
                Script(kind="tool_call", tool_name="fs_write", tool_arguments=write_args),
                Script(kind="stream", content="Wrote hello.txt as requested."),
            ]
        },
        default=Script(kind="stream", content="(unused)"),
    )


async def _wait_for_port_file(data_dir: Path, timeout_secs: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        port_file = data_dir / "port.json"
        if port_file.exists():
            return json.loads(port_file.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        await asyncio.sleep(0.05)
    raise TimeoutError("daemon did not write port.json")


async def _send(ws: Any, message: dict[str, Any]) -> None:
    await ws.send(json.dumps(message))


async def run(workspace: Path, data_dir: Path) -> HarnessResult:
    """Run the harness pass and return every check's outcome."""
    started = time.monotonic()
    result = HarnessResult()
    _prepare_workspace(workspace)

    mock = _mock_provider(workspace)
    daemon = Daemon(data_dir=data_dir, provider=mock)
    daemon_task = asyncio.create_task(daemon.run())
    events: list[dict[str, Any]] = []
    session_id = ""

    try:
        info = await _wait_for_port_file(data_dir)
        async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
            await _send(ws, {"type": "hello", "token": info["token"], "version": 1})
            await ws.recv()  # ready / hello_ack

            await _send(ws, {"type": "open_workspace", "path": str(workspace)})
            state = json.loads(await ws.recv())
            session_id = str(state["session_id"])
            events.append(state)

            # Attach for the replay + live stream, then kick off the turn.
            await _send(ws, {"type": "attach", "session_id": session_id, "from_seq": 2})
            await _send(
                ws,
                {"type": "user_message", "session_id": session_id, "content": "write the greeting"},
            )

            deadline = time.monotonic() + _TURN_TIMEOUT
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                event = json.loads(raw)
                events.append(event)
                # Approval (TD-1401): the approve message is the contract
                # point; the gate itself arrives with TD-802.
                if event.get("type") == "tool_call":
                    await _send(
                        ws,
                        {
                            "type": "approve",
                            "session_id": session_id,
                            "tool_call_id": event["tool_call_id"],
                        },
                    )
                if event.get("type") == "turn_complete":
                    break

            await _send(ws, {"type": "shutdown"})

        await asyncio.wait_for(daemon_task, timeout=10.0)
    finally:
        if not daemon_task.done():
            daemon_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await daemon_task

    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_type.setdefault(str(event.get("type", "")), []).append(event)

    # 1. Workspace open — session running, boundary resolved.
    _check(
        result,
        "workspace open",
        by_type.get("session_state", [{}])[0].get("state") in ("running", "idle"),
        f"state={by_type.get('session_state', [{}])[0].get('state')!r}",
    )
    boundary = by_type.get("boundary_update", [{}])[0]
    _check(
        result,
        "boundary resolved",
        bool(boundary.get("writable_paths")),
        f"source={boundary.get('source')!r}",
    )

    # 2. Steering resolution — the workspace AGENTS.md must reach the
    #    model's system prompt.  (steering_reloaded is a hot-reload event,
    #    TD-509; it never fires on a fresh session's first turn.)
    first_system = ""
    if mock.calls:
        first = mock.calls[0]
        if first.messages and first.messages[0].role == "system":
            first_system = first.messages[0].content or ""
    _check(
        result,
        "steering resolved",
        "Harness workspace" in first_system,
        "workspace AGENTS.md in system prompt" if first_system else "no provider calls recorded",
    )

    # 3+4. Tool call with a classification attached.
    calls = by_type.get("tool_call", [])
    call = calls[0] if calls else {}
    _check(
        result,
        "tool call classified",
        call.get("name") == "fs_write" and call.get("decision_class") in ("A", "B"),
        f"name={call.get('name')!r} class={call.get('decision_class')!r}",
    )

    # 5+6. Execution — successful result carrying the write diff.
    results = by_type.get("tool_result", [])
    tool_result = results[0] if results else {}
    wrote_file = (workspace / "hello.txt").exists() and (workspace / "hello.txt").read_text(
        encoding="utf-8"
    ) == _WRITE_CONTENT
    _check(
        result,
        "execution",
        tool_result.get("status") == "success" and wrote_file,
        f"status={tool_result.get('status')!r} file={'written' if wrote_file else 'missing'}",
    )

    # 7. Checkpoint — the mutation is committed on the session branch.
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

    # 8+9. Ledger + cost — audit rows exist and the turn carried spend.
    aggregates = cost_by_session(AuditStore(data_dir / "audit.db"))
    _check(
        result,
        "ledger",
        any(a.key == session_id and a.prompt_tokens > 0 for a in aggregates),
        f"sessions={len(aggregates)}",
    )
    turn = by_type.get("turn_complete", [{}])[-1]
    _check(
        result,
        "cost accounting",
        float(turn.get("cost", 0.0)) > 0 and int(turn.get("tokens", 0)) > 0,
        f"cost={turn.get('cost')} tokens={turn.get('tokens')}",
    )

    result.elapsed = time.monotonic() - started
    _check(result, "under 60 seconds", result.elapsed < 60.0, f"{result.elapsed:.1f}s")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M1 headless end-to-end harness (TD-1401)")
    parser.add_argument(
        "--workspace", type=Path, default=None, help="workspace dir (default: temp)"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="daemon data dir (default: temp)"
    )
    args = parser.parse_args(argv)

    scratch = tempfile.TemporaryDirectory(prefix="tstd-e2e-")
    root = Path(scratch.name)
    workspace = args.workspace or root / "workspace"
    data_dir = args.data_dir or root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    result = asyncio.run(run(workspace, data_dir))
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
