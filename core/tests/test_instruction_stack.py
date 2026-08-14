"""Instruction-stack query and inspector fields (TD-1201).

The resolved-stack panel lives off ``instruction_stack`` events.  These
tests pin the wire contract: precedence order and per-file token counts,
path-scope match state and what it matched, fallback/shadowed labeling,
imports nested under their importer (flattened with depth), the 200-line
adherence warning, and provider-observed cache state.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tstd.context import ContextAssembler, SteeringFileResolver
from tstd.context.stack import build_instruction_stack
from tstd.cost import CostTracker
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.policy import save_approved_imports
from tstd.protocol import PROTOCOL_VERSION, InstructionStack
from tstd.provider import Usage
from tstd.router import TierRouter
from tstd.session import Session

# ── Helpers ──────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _assemble(home: Path, workspace: Path, matched_paths: set[str] | None = None):
    assembler = ContextAssembler(resolver=SteeringFileResolver(home_dir=home))
    return assembler.assemble_sync(workspace, matched_paths=matched_paths)


async def _connect(uri: str, token: str):
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _running_daemon(tmp: Path) -> tuple[Daemon, asyncio.Task[None]]:
    daemon = Daemon(data_dir=tmp)
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


# ── Cache-state property ─────────────────────────────────────────────────


class TestLastCachedTokens:
    def test_none_before_any_call(self) -> None:
        tracker = CostTracker(make_config())
        assert tracker.last_cached_prompt_tokens is None

    def test_last_main_loop_call_wins(self) -> None:
        config = make_config()
        tracker = CostTracker(config)
        tracker.begin_turn()
        tracker.record(
            "brain",
            Usage(prompt_tokens=100, completion_tokens=10, cached_prompt_tokens=123),
            config.tier("brain"),
        )
        assert tracker.last_cached_prompt_tokens == 123
        # A new turn does not reset the signal — it describes the last
        # observed call until another lands.
        tracker.begin_turn()
        assert tracker.last_cached_prompt_tokens == 123

    def test_classifier_calls_do_not_count(self) -> None:
        config = make_config()
        tracker = CostTracker(config)
        tracker.record_classifier(
            "worker",
            Usage(prompt_tokens=50, completion_tokens=5, cached_prompt_tokens=999),
            config.tier("worker"),
        )
        assert tracker.last_cached_prompt_tokens is None


# ── Builder field forwarding (ACs 1, 3, 4, 5, 6) ────────────────────────


class TestBuilderFields:
    def test_precedence_order_and_per_file_tokens(self, tmp_path: Path) -> None:
        home, ws = tmp_path / "home", tmp_path / "ws"
        _write(home / ".tstdesk" / "AGENTS.md", "home rules\n")
        _write(ws / "AGENTS.md", "root rules\n")
        _write(ws / "sub" / "AGENTS.md", "nested rules\n")
        stack = build_instruction_stack("s1", _assemble(home, ws))

        # Source paths are native strings — normalize separators so the
        # suffix checks hold on Windows too.
        paths = [e.path.replace(os.sep, "/") for e in stack.sources]
        home_idx = next(i for i, p in enumerate(paths) if p.endswith(".tstdesk/AGENTS.md"))
        root_idx = next(i for i, p in enumerate(paths) if p.endswith("ws/AGENTS.md"))
        nested_idx = next(i for i, p in enumerate(paths) if p.endswith("sub/AGENTS.md"))
        assert home_idx < root_idx < nested_idx
        for entry in stack.sources:
            assert entry.tokens > 0
            assert entry.token_method != ""
        assert stack.total_tokens == sum(e.tokens for e in stack.sources if e.active)

    def test_claude_md_fallback_and_shadow_labels(self, tmp_path: Path) -> None:
        # Fallback: CLAUDE.md alone is used, labeled as the fallback.
        home, ws = tmp_path / "home", tmp_path / "ws"
        _write(ws / "CLAUDE.md", "claude rules\n")
        stack = build_instruction_stack("s1", _assemble(home, ws))
        entry = next(e for e in stack.sources if e.path.endswith("CLAUDE.md"))
        assert entry.is_fallback is True

        # Shadow: AGENTS.md outranks a sibling CLAUDE.md; the winner names
        # the shadowed file and the loser is not a separate source.
        home2, ws2 = tmp_path / "home2", tmp_path / "ws2"
        _write(ws2 / "AGENTS.md", "agents rules\n")
        _write(ws2 / "CLAUDE.md", "claude rules\n")
        stack2 = build_instruction_stack("s1", _assemble(home2, ws2))
        winner = next(e for e in stack2.sources if e.path.endswith("AGENTS.md"))
        assert winner.shadowed_path is not None
        assert winner.shadowed_path.endswith("CLAUDE.md")
        assert not any(
            e.path.endswith("CLAUDE.md") and e.path != winner.shadowed_path for e in stack2.sources
        )

    def test_path_scoped_rules_show_match_state_and_globs(self, tmp_path: Path) -> None:
        home, ws = tmp_path / "home", tmp_path / "ws"
        _write(ws / "AGENTS.md", "root\n")
        _write(
            ws / ".tst" / "rules" / "api.md",
            "---\nappliesTo:\n  - src/**\n---\napi rules\n",
        )
        _write(
            ws / ".tst" / "rules" / "docs.md",
            "---\nappliesTo:\n  - docs/**\n---\ndocs rules\n",
        )
        stack = build_instruction_stack("s1", _assemble(home, ws, {"src/app.py"}))

        api = next(e for e in stack.sources if e.path.endswith("api.md"))
        docs = next(e for e in stack.sources if e.path.endswith("docs.md"))
        assert api.active is True
        assert api.applies_to == ["src/**"]
        assert docs.active is False
        assert docs.applies_to == ["docs/**"]
        # Inactive rules cost nothing.
        assert stack.total_tokens == sum(e.tokens for e in stack.sources if e.active)

    def test_imports_flattened_with_depth(self, tmp_path: Path) -> None:
        home, ws = tmp_path / "home", tmp_path / "ws"
        _write(ws / "AGENTS.md", "root\n@extra.md\n")
        _write(ws / "extra.md", "extra\n@more.md\n")
        _write(ws / "more.md", "more\n")
        stack = build_instruction_stack("s1", _assemble(home, ws))

        root = next(e for e in stack.sources if e.path.endswith("AGENTS.md"))
        by_path = {Path(i.path).name: i for i in root.imports}
        assert by_path["extra.md"].depth == 1
        assert by_path["more.md"].depth == 2
        assert by_path["extra.md"].issue is None

    def test_long_file_carries_adherence_warning(self, tmp_path: Path) -> None:
        home, ws = tmp_path / "home", tmp_path / "ws"
        _write(ws / "AGENTS.md", "\n".join(f"line {i}" for i in range(201)))
        stack = build_instruction_stack("s1", _assemble(home, ws))
        entry = next(e for e in stack.sources if e.path.endswith("AGENTS.md"))
        assert any("exceeds 200 lines" in w for w in entry.warnings)

    def test_last_cached_tokens_passes_through(self, tmp_path: Path) -> None:
        home, ws = tmp_path / "home", tmp_path / "ws"
        _write(ws / "AGENTS.md", "root\n")
        steering = _assemble(home, ws)
        assert build_instruction_stack("s1", steering).last_cached_tokens is None
        assert (
            build_instruction_stack("s1", steering, last_cached_tokens=77).last_cached_tokens == 77
        )


# ── Daemon query handler ─────────────────────────────────────────────────


class TestGetInstructionStackHandler:
    @pytest.mark.asyncio
    async def test_unknown_session_is_session_not_found(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _running_daemon(Path(tmp))
            try:
                ws = await _connect(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await ws.send(json.dumps({"type": "get_instruction_stack", "session_id": "nope"}))
                resp = json.loads(await ws.recv())
                assert resp["type"] == "error"
                assert resp["code"] == "session_not_found"
                await ws.close()
            finally:
                daemon._shutdown_event.set()
                daemon_task.cancel()

    @pytest.mark.asyncio
    async def test_happy_path_returns_stack(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        _write(workspace / "AGENTS.md", "workspace rules\n")
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _running_daemon(Path(tmp))
            try:
                ws = await _connect(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await ws.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
                opened = json.loads(await ws.recv())
                assert opened["type"] == "session_state"
                session_id = opened["session_id"]

                await ws.send(
                    json.dumps({"type": "get_instruction_stack", "session_id": session_id})
                )
                resp = json.loads(await ws.recv())
                assert resp["type"] == "instruction_stack"
                assert resp["session_id"] == session_id
                assert any(e["path"] == str(workspace / "AGENTS.md") for e in resp["sources"])
                # No turn yet — cache state is honestly unknown.
                assert resp["last_cached_tokens"] is None
                await ws.close()
            finally:
                daemon._shutdown_event.set()
                daemon_task.cancel()

    @pytest.mark.asyncio
    async def test_approved_external_import_shows_clean(self, tmp_path: Path) -> None:
        """The durable allowlist reaches the panel: an approved import no
        longer carries the "awaiting approval" issue (TD-505)."""
        workspace = tmp_path / "ws"
        external = (tmp_path / "shared.md").resolve()
        _write(workspace / "AGENTS.md", f"workspace rules\n@{external}\n")
        _write(external, "shared rules\n")
        save_approved_imports(workspace, [external])
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _running_daemon(Path(tmp))
            try:
                ws = await _connect(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await ws.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
                opened = json.loads(await ws.recv())
                assert opened["type"] == "session_state"

                await ws.send(
                    json.dumps(
                        {"type": "get_instruction_stack", "session_id": opened["session_id"]}
                    )
                )
                resp = json.loads(await ws.recv())
                assert resp["type"] == "instruction_stack"
                root = next(e for e in resp["sources"] if e["path"].endswith("ws/AGENTS.md"))
                imp = next(i for i in root["imports"] if i["path"] == str(external))
                assert imp["issue"] is None
                await ws.close()
            finally:
                daemon._shutdown_event.set()
                daemon_task.cancel()


# ── Hot-reload emission carries cache state ──────────────────────────────


class TestReloadEmission:
    @pytest.mark.asyncio
    async def test_reload_stack_reports_last_cached_tokens(self, tmp_path: Path) -> None:
        _write(tmp_path / "AGENTS.md", "v1 rules\n")
        session = Session(str(tmp_path))
        mock = MockProvider(default=Script(kind="text", content="ok", cached_tokens=321))
        await start_loop(session, TierRouter(lead_turns=3), mock, make_config(), None, None)

        await session.add_user_message("hi")
        await wait_for_turn(session, 1)

        # Steering change → turn 2 emits a fresh stack before the call,
        # so the cache figure is turn 1's observed value.
        _write(tmp_path / "AGENTS.md", "v2 rules — changed\n")
        await session.add_user_message("again")
        await wait_for_turn(session, 2)

        stacks = [e for e in session.event_log.all_events if isinstance(e, InstructionStack)]
        assert len(stacks) >= 1
        assert stacks[-1].last_cached_tokens == 321
