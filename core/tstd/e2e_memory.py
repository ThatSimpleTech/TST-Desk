"""Memory headless harness — the M4 exit (TD-2701).

Same job as TD-1401, for memory: a scripted session loads a matching
topic into the brain prompt only, distills a proposal, and accept or
reject is the write gate.

Does not go through ``e2e_harness.run``. That pass is frozen on
hello.txt. This module reuses the protocol-client helpers and the
:class:`HarnessResult` report, then drives ``end_session`` plus
``memory_accept`` / ``memory_reject``.

Distill is a worker ``chat_completion`` with ``DISTILL_SYSTEM_PROMPT``,
not a PromptAssembler pass. Two Class A writes exhaust lead-turns so a
real worker assemble is recorded. Embeddings use an empty ``base_url``
so CI never needs a sidecar.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from .config import EmbeddingsConfig, ModelConfig, Preset, TierConfig
from .daemon import Daemon
from .e2e_checks import HarnessResult
from .e2e_harness import _send, _wait_for_port_file
from .e2e_memory_checks import git, verify_memory, workspace_bytes
from .e2e_plan import MemoryHarnessPlan, MemoryResolution
from .memory_store import memory_dir, scaffold_workspace_memory
from .mock import MockProvider, Script

_TOPIC = "xylophone-lint-pin"
_TOPIC_NAME = "xylophone.md"
_TOPIC_TEXT = f"# Xylophone\n\n{_TOPIC} is the durable convention.\n"
_INDEX_TEXT = "durable: harness index only\n"
_ACCEPT_REL = ".tst/memory/MEMORY.md"
_ACCEPT_TEXT = f"durable: {_TOPIC} recorded from the harness turn\n"
_STEERING = "# Memory harness\n\nKeep replies short.\n"
# Same carve-out the product ships: runtime under .tst/ is ignored,
# memory and rules are not. Without this the harness repo itself
# reports config/autonomy/scratch as untracked and reject cannot
# see a clean status.
_GITIGNORE = ".tst/*\n!.tst/rules/\n!.tst/rules/**\n!.tst/memory/\n!.tst/memory/**\n"
_PROMPT = "please follow the xylophone convention"
_BRAIN = "harness-brain"
_WORKER = "harness-worker"
_VALIDATOR = "harness-validator"
_TURN_TIMEOUT = 45.0
_BUDGET = 60.0


def _tier(slug: str) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url="http://127.0.0.1:9/v1",
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=8_000,
        max_output_tokens=1_000,
    )


def memory_config() -> ModelConfig:
    """Distinct slugs per tier; embeddings off. No discovery, no sidecar."""
    return ModelConfig(
        presets={
            "memory-harness": Preset(
                brain=_tier(_BRAIN),
                worker=_tier(_WORKER),
                validator=_tier(_VALIDATOR),
            )
        },
        active_preset="memory-harness",
        embeddings=EmbeddingsConfig(base_url="", model=""),
    )


def _provider(workspace: Path) -> MockProvider:
    """Two Class A writes exhaust lead-turns so a worker assemble runs.

    The scratch path is under ``.tst/`` and gitignored, so reject can
    still see a clean ``git status``. Distill is a later worker
    ``chat_completion``, keyed on the same slug after the stream.
    """
    scratch_path = workspace / ".tst" / "harness-scratch.txt"
    scratch = json.dumps({"path": str(scratch_path), "content": "x\n"})
    distill = json.dumps(
        {"changes": [{"action": "replace", "path": "MEMORY.md", "content": _ACCEPT_TEXT}]}
    )
    return MockProvider(
        sequences={
            _BRAIN: [
                Script(kind="tool_call", tool_name="fs_write", tool_arguments=scratch),
                Script(kind="tool_call", tool_name="fs_write", tool_arguments=scratch),
            ],
            _WORKER: [
                Script(kind="stream", content="Noted the convention."),
                Script(kind="text", content=distill),
            ],
        },
        default=Script(kind="stream", content="(unused)"),
    )


def memory_plan(workspace: Path) -> MemoryHarnessPlan:
    """TD-2701's plan: scripted, offline, heading-match only."""
    return MemoryHarnessPlan(
        provider=_provider(workspace),
        steering=_STEERING,
        prompt=_PROMPT,
        topic=_TOPIC,
        memory_files={"MEMORY.md": _INDEX_TEXT, _TOPIC_NAME: _TOPIC_TEXT},
        accept_relpath=_ACCEPT_REL,
        accept_content=_ACCEPT_TEXT,
        brain_slug=_BRAIN,
        worker_slug=_WORKER,
        turn_timeout=_TURN_TIMEOUT,
        budget_secs=_BUDGET,
    )


def _prepare_workspace(workspace: Path, plan: MemoryHarnessPlan) -> None:
    """Git baseline with steering plus the seeded memory tree."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(plan.steering, encoding="utf-8")
    (workspace / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    scaffold_workspace_memory(workspace)
    root = memory_dir(workspace)
    for name, text in plan.memory_files.items():
        (root / name).write_text(text, encoding="utf-8")

    def run_git(*args: str) -> None:
        proc = git(
            workspace,
            "-c",
            "user.name=TST Desk Harness",
            "-c",
            "user.email=harness@localhost",
            *args,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or "git failed")

    run_git("init", "-q")
    run_git("add", "AGENTS.md", ".gitignore", ".tst/memory")
    run_git("commit", "-q", "-m", "baseline")


async def run_memory(
    workspace: Path,
    data_dir: Path,
    *,
    resolution: MemoryResolution,
    plan: MemoryHarnessPlan | None = None,
) -> HarnessResult:
    """Drive one memory pass. *resolution* is accept or the reject sister."""
    started = time.monotonic()
    plan = plan or memory_plan(workspace)
    _prepare_workspace(workspace, plan)

    config = memory_config()
    daemon = Daemon(data_dir=data_dir, provider=plan.provider)
    daemon.config = config
    daemon_task = asyncio.create_task(daemon.run())
    events: list[dict[str, Any]] = []
    before: dict[str, bytes] = {}
    head_before = ""

    try:
        info = await _wait_for_port_file(data_dir)
        async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
            await _send(ws, {"type": "hello", "token": info["token"], "version": 1})
            await ws.recv()

            await _send(ws, {"type": "open_workspace", "path": str(workspace)})
            state = json.loads(await ws.recv())
            session_id = str(state["session_id"])
            events.append(state)

            await _send(ws, {"type": "attach", "session_id": session_id, "from_seq": 2})
            await _send(
                ws,
                {"type": "user_message", "session_id": session_id, "content": plan.prompt},
            )

            deadline = time.monotonic() + plan.turn_timeout
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                events.append(event)
                if event.get("type") == "approval_request":
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

            await _send(ws, {"type": "end_session", "session_id": session_id})
            proposal: dict[str, Any] | None = None
            distill_deadline = time.monotonic() + plan.turn_timeout
            while time.monotonic() < distill_deadline:
                remaining = max(0.1, distill_deadline - time.monotonic())
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                events.append(event)
                if event.get("type") == "memory_proposal":
                    proposal = event
                    break

            before = workspace_bytes(workspace)
            head_before = git(workspace, "rev-parse", "HEAD").stdout.strip()
            if proposal is not None:
                verb = "memory_accept" if resolution == "accept" else "memory_reject"
                await _send(
                    ws,
                    {
                        "type": verb,
                        "session_id": session_id,
                        "proposal_id": proposal["proposal_id"],
                    },
                )
                if resolution == "accept":
                    listed_deadline = time.monotonic() + 10.0
                    while time.monotonic() < listed_deadline:
                        remaining = max(0.1, listed_deadline - time.monotonic())
                        listed = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                        events.append(listed)
                        if listed.get("type") == "memory_files":
                            break

            await _send(ws, {"type": "shutdown"})

        await asyncio.wait_for(daemon_task, timeout=10.0)
    finally:
        if not daemon_task.done():
            daemon_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await daemon_task

    return verify_memory(
        events=events,
        plan=plan,
        workspace=workspace,
        config=config,
        resolution=resolution,
        before=before,
        head_before=head_before,
        started=started,
    )
