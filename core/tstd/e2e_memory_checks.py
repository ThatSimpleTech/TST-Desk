"""Verdict half of the M4 memory harness (TD-2701).

``e2e_memory`` drives the session; this module decides what the
transcript and the workspace prove. Distill is identified by
``DISTILL_SYSTEM_PROMPT``, never treated as a PromptAssembler worker
prompt.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from .config import ModelConfig
from .context import PromptAssembler
from .context.memory_loader import load_memory_for_task
from .e2e_checks import HarnessResult
from .e2e_plan import MemoryHarnessPlan, MemoryResolution
from .memory_commit import MEMORY_COMMIT_SUBJECT
from .memory_distill import DISTILL_SYSTEM_PROMPT
from .provider import ChatCompletionRequest, content_as_text


def git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def workspace_bytes(workspace: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for path in workspace.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        out[path.relative_to(workspace).as_posix()] = path.read_bytes()
    return out


def _system_text(request: ChatCompletionRequest) -> str:
    if request.messages and request.messages[0].role == "system":
        return content_as_text(request.messages[0].content)
    return ""


def _check(result: HarnessResult, name: str, ok: bool, detail: str = "") -> None:
    result.checks.append((name, ok, detail))


def _verify_prompts(
    result: HarnessResult, plan: MemoryHarnessPlan, workspace: Path, config: ModelConfig
) -> None:
    _check(
        result,
        "embeddings disabled",
        config.embeddings.base_url == "" and config.embeddings.model == "",
        f"base_url={config.embeddings.base_url!r}",
    )

    brain_sys = ""
    worker_sys = ""
    for call in plan.provider.calls:
        system = _system_text(call)
        if call.model == plan.brain_slug and system and not brain_sys:
            brain_sys = system
        if (
            call.model == plan.worker_slug
            and system
            and DISTILL_SYSTEM_PROMPT not in system
            and not worker_sys
        ):
            worker_sys = system

    _check(
        result,
        "brain prompt has topic",
        bool(brain_sys) and plan.topic in brain_sys,
        "recorded brain system prompt" if brain_sys else "no brain system prompt recorded",
    )
    _check(
        result,
        "recorded worker has no topic",
        bool(worker_sys) and plan.topic not in worker_sys,
        "in-loop worker assemble" if worker_sys else "no in-loop worker assemble",
    )

    loaded = load_memory_for_task(workspace, plan.prompt)
    assembler = PromptAssembler(workspace)
    worker = assembler.assemble_sync("worker", task=plan.prompt, memory=loaded.block)
    validator = assembler.assemble_sync("validator", diff="+x", memory=loaded.block)
    _check(
        result,
        "worker assemble has no topic",
        plan.topic not in worker.text,
        "PromptAssembler worker",
    )
    _check(
        result,
        "validator assemble has no topic",
        plan.topic not in validator.text,
        "PromptAssembler validator",
    )


def _verify_resolution(
    result: HarnessResult,
    *,
    workspace: Path,
    plan: MemoryHarnessPlan,
    resolution: MemoryResolution,
    before: dict[str, bytes],
    head_before: str,
    proposal: dict[str, Any] | None,
) -> None:
    _check(
        result,
        "distill proposal",
        bool(proposal) and bool((proposal or {}).get("files")),
        f"proposal_id={(proposal or {}).get('proposal_id')!r}",
    )
    target = workspace / plan.accept_relpath
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    porcelain = git(workspace, "status", "--porcelain").stdout
    head = git(workspace, "log", "-1", "--format=%s").stdout.strip()
    if resolution == "accept":
        _check(
            result,
            "accept wrote memory",
            text == plan.accept_content,
            f"path={plan.accept_relpath}",
        )
        _check(
            result,
            "memory commit",
            head == MEMORY_COMMIT_SUBJECT,
            f"subject={head!r}",
        )
        return
    after = workspace_bytes(workspace)
    head_after = git(workspace, "rev-parse", "HEAD").stdout.strip()
    _check(
        result,
        "reject leaves tree identical",
        after == before,
        "workspace bytes unchanged" if after == before else "workspace bytes changed",
    )
    _check(
        result,
        "reject git status clean",
        porcelain == "" and head_after == head_before,
        "clean" if porcelain == "" else porcelain.strip(),
    )


def verify_memory(
    *,
    events: list[dict[str, Any]],
    plan: MemoryHarnessPlan,
    workspace: Path,
    config: ModelConfig,
    resolution: MemoryResolution,
    before: dict[str, bytes],
    head_before: str,
    started: float,
) -> HarnessResult:
    """Judge one memory pass from the events and tree it produced."""
    result = HarnessResult()
    state = next((e for e in events if e.get("type") == "session_state"), {})
    _check(
        result,
        "workspace open",
        state.get("state") in ("running", "idle"),
        f"state={state.get('state')!r}",
    )
    _verify_prompts(result, plan, workspace, config)
    proposal = next((e for e in events if e.get("type") == "memory_proposal"), None)
    _verify_resolution(
        result,
        workspace=workspace,
        plan=plan,
        resolution=resolution,
        before=before,
        head_before=head_before,
        proposal=proposal,
    )
    result.elapsed = time.monotonic() - started
    _check(
        result,
        f"under {int(plan.budget_secs)} seconds",
        result.elapsed < plan.budget_secs,
        f"{result.elapsed:.1f}s",
    )
    return result
