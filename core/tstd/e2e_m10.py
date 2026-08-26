"""M10 exit harness (TD-4604). Mock MCP, slash/skill only after invoke.

Interactive, not ``e2e_harness.run``. This is the M10 exit criterion.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml
from websockets.asyncio.client import connect

from .config import (
    EmbeddingsConfig,
    McpConfig,
    McpServerConfig,
    ModelConfig,
    Preset,
    TierConfig,
    cached_config,
    load_config,
)
from .daemon import Daemon
from .e2e_checks import HarnessResult, _check
from .e2e_harness import _send, _wait_for_port_file
from .e2e_m5 import _hello, _recv_event
from .mock import MockProvider, Script
from .provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ProviderError,
    Usage,
)

_STEERING = "# M10 harness\n\nKeep replies short.\n"
_SLASH_BODY = "M10_SLASH_BODY_UNIQUE"
_SKILL_BODY = "M10_SKILL_BODY_UNIQUE"
_SKILL_DESC = "M10_SKILL_DESC_UNIQUE"
_HELLO = "hello m10"
_MCP_PROMPT = "echo via mcp"
_BRAIN = "m10-brain"
_WORKER = "m10-worker"
_VALIDATOR = "m10-validator"
_BUDGET = 60.0
_TURN = 20.0
_MCP_TOOL = "harness__echo"

# Copied from tests/test_mcp_load.py — a newline-delimited MCP speaker.
_FAKE_STDIO = r"""
import json, sys
TOOL = sys.argv[1] if len(sys.argv) > 1 else "echo"
def _read():
    line = sys.stdin.readline()
    if not line:
        return None
    parsed = json.loads(line)
    return parsed if isinstance(parsed, dict) else None
def _write(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
while True:
    msg = _read()
    if msg is None:
        break
    method, req_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        _write({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"}}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        _write({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [{
            "name": TOOL, "description": "scripted echo",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                            "required": ["text"]}}]}})
    elif method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        _write({"jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": str(args.get("text", ""))}]}})
"""


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


def m10_config(script: Path) -> ModelConfig:
    """Known slugs plus one stdio MCP server (``harness__echo``)."""
    return ModelConfig(
        presets={
            "m10-harness": Preset(
                brain=_tier(_BRAIN),
                worker=_tier(_WORKER),
                validator=_tier(_VALIDATOR),
            )
        },
        active_preset="m10-harness",
        embeddings=EmbeddingsConfig(base_url="", model=""),
        mcp=McpConfig(
            servers={
                "harness": McpServerConfig(
                    transport="stdio",
                    command=[sys.executable, str(script), "echo"],
                )
            }
        ),
    )


def _prepare(workspace: Path, data_dir: Path) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(_STEERING, encoding="utf-8")
    commands = workspace / ".tst" / "commands"
    commands.mkdir(parents=True, exist_ok=True)
    (commands / "review.md").write_text(f"{_SLASH_BODY}\n", encoding="utf-8")
    skill = workspace / ".tst" / "skills" / "pack"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\ndescription: {_SKILL_DESC}\nwhenToUse: when the harness asks\n---\n{_SKILL_BODY}\n",
        encoding="utf-8",
    )
    script = data_dir / "fake_mcp.py"
    script.write_text(_FAKE_STDIO, encoding="utf-8")
    (data_dir / "config.yaml").write_text(
        yaml.safe_dump(m10_config(script).model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    cached_config.cache_clear()
    return script


class _SequentialMock(MockProvider):
    """Play scripts in order on any slug. Classifier worker calls do not consume."""

    def __init__(self, steps: list[Script]) -> None:
        super().__init__(default=Script(kind="stream", content="(unused)"))
        self._steps = steps
        self._pos = 0

    def _script_for(self, model: str) -> Script:
        if self._pos < len(self._steps):
            script = self._steps[self._pos]
            self._pos += 1
            return script
        return self._default

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError:
        # Classifier worker: letter only; do not consume the script tape.
        if not request.tools and (request.max_tokens or 0) <= 8:
            self.calls.append(request)
            return ChatCompletionResponse(
                id="mock-classify",
                model=request.model,
                message=ChatMessage(role="assistant", content="B"),
                finish_reason="stop",
                usage=Usage(completion_tokens=1, total_tokens=1),
            )
        return await super().chat_completion(request)


def _provider() -> MockProvider:
    load = json.dumps({"name": "pack"})
    echo = json.dumps({"text": "m10"})
    return _SequentialMock(
        [
            Script(kind="stream", content="Ready."),
            Script(kind="tool_call", tool_name="load_skill", tool_arguments=load),
            Script(kind="stream", content="Skill loaded."),
            Script(kind="tool_call", tool_name=_MCP_TOOL, tool_arguments=echo),
            Script(kind="stream", content="Echoed."),
        ]
    )


def _system_texts(provider: MockProvider) -> list[str]:
    texts: list[str] = []
    for request in provider.calls:
        for message in request.messages:
            if message.role == "system" and message.content:
                texts.append(message.content)
                break
    return texts


def _user_texts(events: list[dict[str, Any]], provider: MockProvider) -> list[str]:
    texts = [str(e.get("content") or "") for e in events if e.get("type") == "user_turn"]
    texts.extend(
        m.content for req in provider.calls for m in req.messages if m.role == "user" and m.content
    )
    return texts


def _prefix_of(system: str) -> str:
    """Cache-prefix slice — skills sit after ``## Skills`` / ``## Skill:``."""
    cut = len(system)
    for marker in ("## Skills", "## Skill:"):
        idx = system.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return system[:cut]


async def _wait_turn(
    ws: Any, prompt: str, timeout_secs: float
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    events: list[dict[str, Any]] = []
    seen_user = False
    deadline = time.monotonic() + timeout_secs
    session_id = ""
    while time.monotonic() < deadline:
        event = await _recv_event(ws, max(0.1, deadline - time.monotonic()))
        events.append(event)
        if event.get("type") == "error":
            return events, event
        if event.get("type") == "user_turn" and event.get("content") == prompt:
            seen_user = True
        if event.get("type") == "approval_request":
            session_id = str(event.get("session_id") or session_id)
            await _send(
                ws,
                {
                    "type": "approve",
                    "session_id": session_id,
                    "tool_call_id": event["tool_call_id"],
                },
            )
        if event.get("type") == "turn_complete" and seen_user:
            return events, None
    return events, {"type": "error", "error": "turn timed out"}


def _verify(
    result: HarnessResult,
    events: list[dict[str, Any]],
    provider: MockProvider,
    error: dict[str, Any] | None,
) -> None:
    systems = _system_texts(provider)
    users = _user_texts(events, provider)
    first = systems[0] if systems else ""
    later = systems[1:]
    catalog = _SKILL_DESC in first or "`pack`" in first or "pack" in first
    _check(
        result,
        "catalog before invoke",
        catalog and _SKILL_BODY not in first and _SLASH_BODY not in first,
        f"n={len(systems)} catalog={catalog}",
    )
    _check(
        result,
        "slash after invoke",
        any(_SLASH_BODY in text for text in users) or any(_SLASH_BODY in text for text in later),
        f"users={sum(1 for t in users if _SLASH_BODY in t)}",
    )
    prefix_clean = all(_SKILL_BODY not in _prefix_of(text) for text in systems)
    _check(
        result,
        "skill body after load",
        any(_SKILL_BODY in text for text in later) and _SKILL_BODY not in first and prefix_clean,
        f"later={len(later)} prefix_clean={prefix_clean}",
    )
    echo_calls = [e for e in events if e.get("type") == "tool_call" and e.get("name") == _MCP_TOOL]
    echo_ids = {e.get("tool_call_id") for e in echo_calls}
    echo_logged = [
        e
        for e in events
        if e.get("type") == "decision_logged" and _MCP_TOOL in str(e.get("what") or "")
    ]
    echo_results = [
        e for e in events if e.get("type") == "tool_result" and e.get("tool_call_id") in echo_ids
    ]
    approvals = [
        e for e in events if e.get("type") == "approval_request" and e.get("tool_name") == _MCP_TOOL
    ]
    classified = echo_calls + echo_logged + approvals
    classes = [e.get("decision_class") for e in classified]
    classed = bool(classes) and all(c in {"B", "C"} for c in classes)
    ran = any(e.get("status") == "success" for e in echo_results) or bool(approvals)
    failed = any(e.get("type") == "turn_complete" and e.get("failed") for e in events)
    unclassified = error is not None and "Unclassified" in str(error)
    _check(
        result,
        "mcp classified",
        classed and not unclassified and not failed,
        f"classes={classes} error={None if error is None else error.get('error')}",
    )
    _check(
        result,
        "mcp through classifier",
        classed and ran,
        f"calls={len(echo_calls)} results={len(echo_results)} approvals={len(approvals)}",
    )


async def run_m10(workspace: Path, data_dir: Path) -> HarnessResult:
    """Drive the M10 exit: slash, skill, and a classified MCP tool."""
    started = time.monotonic()
    _prepare(workspace, data_dir)
    home = data_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    provider = _provider()
    events: list[dict[str, Any]] = []
    error: dict[str, Any] | None = None
    daemon: Daemon | None = None
    daemon_task: asyncio.Task[None] | None = None

    def _harness_config(*_args: object, **_kwargs: object) -> ModelConfig:
        return load_config(data_dir / "config.yaml")

    try:
        with (
            patch.dict(os.environ, {"HOME": str(home)}, clear=False),
            patch("tstd.daemon.cached_config", _harness_config),
        ):
            daemon = Daemon(data_dir=data_dir, provider=provider)
            daemon.config = _harness_config()
            daemon_task = asyncio.create_task(daemon.run())
            info = await _wait_for_port_file(data_dir)
            async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
                await _hello(ws, str(info["token"]))
                await _send(ws, {"type": "open_workspace", "path": str(workspace)})
                opened = await _recv_event(ws, 15.0)
                events.append(opened)
                session_id = str(opened.get("session_id") or "")
                await _send(ws, {"type": "attach", "session_id": session_id, "from_seq": 1})
                # Pin brain so post-load_skill stays on the catalog+body prompt.
                await _send(ws, {"type": "set_tier", "session_id": session_id, "tier": "brain"})
                for prompt in (_HELLO, _SLASH_BODY, _MCP_PROMPT):
                    await _send(
                        ws,
                        {"type": "user_message", "session_id": session_id, "content": prompt},
                    )
                    turn_events, turn_error = await _wait_turn(ws, prompt, _TURN)
                    events.extend(turn_events)
                    if turn_error is not None:
                        error = turn_error
                        break
                await ws.close()
    finally:
        if daemon_task is not None and not daemon_task.done():
            if daemon is not None:
                daemon._shutdown_event.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(daemon_task, timeout=10.0)
            if not daemon_task.done():
                daemon_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await daemon_task
        cached_config.cache_clear()

    result = HarnessResult(elapsed=time.monotonic() - started)
    result.notes.append("TD-4604 this harness is the M10 exit criterion")
    _verify(result, events, provider, error)
    _check(result, "under budget", result.elapsed < _BUDGET, f"{result.elapsed:.1f}s")
    _check(result, "m10 exit criterion", result.ok, "slash + skill + classified MCP")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M10 exit harness — mock pass (TD-4604)")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    scratch = tempfile.TemporaryDirectory(prefix="tstd-e2e-m10-")
    root = Path(scratch.name)
    workspace = args.workspace or root / "workspace"
    data_dir = args.data_dir or root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_m10(workspace, data_dir))
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
