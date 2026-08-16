"""End-to-end headless harness (TD-1401, live leg TD-1803).

Runs one scripted session with no UI and checks the full chain the
milestone promises: workspace open → steering resolution → user message →
tool call → classification → approval → execution → checkpoint → ledger →
cost accounting.

The daemon runs in-process but over the real WebSocket protocol — the
harness is a protocol client, not a unit test.

This module drives the session and collects its events; ``e2e_checks``
decides what they prove (split under TD-1807).

A :class:`HarnessPlan` says what to run the pass against.  The default
plan is TD-1401's: a scripted :class:`MockProvider`, deterministic and
offline, finishing well inside the sixty-second CI budget.  TD-1803 adds a
live plan (``tstd.e2e_live``) driven by a real OpenAI-compatible endpoint.
The mock plan's behaviour is frozen — the live leg was added by making the
differences data rather than by branching this module.

Approval note: TD-802's gate registers a pending approval when it emits
``approval_request``, so that is the event the live plan approves on.  The
mock plan keeps approving on ``tool_call``: its scripted in-workspace
absolute write classifies A and runs automatically, so no gate ever opens
and the message is a deliberate no-op that pins the contract point.
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
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from .config import ModelDiscoveryError, cached_config
from .daemon import Daemon
from .discovery import discover_model
from .e2e_checks import HarnessResult, verify
from .e2e_live import (
    LiveProvider,
    ProviderContractError,
    live_plan,
    live_preflight,
    raise_on_contract_failure,
)
from .e2e_plan import HarnessPlan
from .mock import MockProvider, Script
from .policy import save_policy

_STEERING_TEXT = "# Harness workspace\n\nKeep the greeting in hello.txt short.\n"
_WRITE_CONTENT = "hello from M1\n"

# Per-check timeout: a healthy run finishes in a couple of seconds; the
# story budget is sixty seconds for the whole pass including startup.
_TURN_TIMEOUT = 45.0


def _prepare_workspace(workspace: Path, plan: HarnessPlan) -> None:
    """A clean git baseline with one steering file (AGENTS.md)."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(plan.steering, encoding="utf-8")
    if plan.policy is not None:
        save_policy(workspace, plan.policy)

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
    brain = cached_config().tier("brain").require_slug()
    return MockProvider(
        sequences={
            brain: [
                Script(kind="tool_call", tool_name="fs_write", tool_arguments=write_args),
                Script(kind="stream", content="Wrote hello.txt as requested."),
            ]
        },
        default=Script(kind="stream", content="(unused)"),
    )


def mock_plan(workspace: Path) -> HarnessPlan:
    """TD-1401's plan: scripted, offline, and the default.

    Frozen behaviour — the live leg (TD-1803) was added alongside it, never
    on top of it.
    """
    return HarnessPlan(
        provider=_mock_provider(workspace),
        steering=_STEERING_TEXT,
        prompt="write the greeting",
        approve_on="tool_call",
        content_ok=lambda text: text == _WRITE_CONTENT,
        expect_spend=True,
        turn_timeout=_TURN_TIMEOUT,
        budget_secs=60.0,
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


async def run(workspace: Path, data_dir: Path, plan: HarnessPlan | None = None) -> HarnessResult:
    """Run the harness pass and return every check's outcome.

    *plan* defaults to :func:`mock_plan`, so the TD-1401 call shape and
    result are unchanged.
    """
    started = time.monotonic()
    plan = plan or mock_plan(workspace)
    _prepare_workspace(workspace, plan)

    daemon = Daemon(data_dir=data_dir, provider=plan.provider)
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
                {"type": "user_message", "session_id": session_id, "content": plan.prompt},
            )

            deadline = time.monotonic() + plan.turn_timeout
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                event = json.loads(raw)
                events.append(event)
                # TD-802 registers the pending approval when it emits
                # ``approval_request``; approving on ``tool_call`` answers a
                # gate that has not opened.  Which event carries the pending
                # approval is the plan's call — see HarnessPlan.approve_on.
                if event.get("type") == plan.approve_on:
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

    return await verify(
        events=events,
        plan=plan,
        workspace=workspace,
        data_dir=data_dir,
        session_id=session_id,
        started=started,
    )


def _run_live(workspace: Path, data_dir: Path, endpoint: str, model: str | None) -> int:
    """Drive one live pass.  0 pass, 1 checks failed, 2 not run, 3 provider broke."""

    async def _go() -> int:
        # The tier may leave its slug unset (TD-1805); resolving it here is
        # what the daemon is about to do anyway, and a failure is a "not
        # run" like any other absent-server reason, never a loop failure.
        try:
            slug = model or cached_config().tier("brain").slug or await discover_model(endpoint)
        except ModelDiscoveryError as e:
            print(f"SKIP  live harness not run: {e}")
            return 2

        reason = await live_preflight(endpoint, slug)
        if reason is not None:
            print(f"SKIP  live harness not run: {reason}")
            return 2

        provider = LiveProvider(endpoint)
        try:
            result = await run(workspace, data_dir, live_plan(workspace, provider))
        finally:
            await provider.aclose()
        print(result.report())

        # Attribution before verdict: a provider failure fails every check
        # downstream of it, so blaming the loop first would be wrong.
        try:
            raise_on_contract_failure(provider)
        except ProviderContractError as e:
            print(f"PROVIDER CONTRACT  {e}")
            return 3
        return 0 if result.ok else 1

    return asyncio.run(_go())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Headless end-to-end harness — mock by default (TD-1401), "
        "live with --live-endpoint (TD-1803)"
    )
    parser.add_argument(
        "--workspace", type=Path, default=None, help="workspace dir (default: temp)"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="daemon data dir (default: temp)"
    )
    parser.add_argument(
        "--live-endpoint",
        default=None,
        help="run against this OpenAI-compatible loopback endpoint instead of the mock",
    )
    parser.add_argument(
        "--live-model",
        default=None,
        help="slug the endpoint must serve (default: the active preset's brain tier)",
    )
    args = parser.parse_args(argv)
    if args.live_endpoint is None and args.live_model is not None:
        parser.error("--live-model has no effect without --live-endpoint")

    scratch = tempfile.TemporaryDirectory(prefix="tstd-e2e-")
    root = Path(scratch.name)
    workspace = args.workspace or root / "workspace"
    data_dir = args.data_dir or root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    if args.live_endpoint is not None:
        return _run_live(workspace, data_dir, args.live_endpoint, args.live_model)

    result = asyncio.run(run(workspace, data_dir))
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
