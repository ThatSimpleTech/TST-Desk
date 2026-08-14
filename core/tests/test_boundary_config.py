"""Tests for workspace boundary configuration (TD-706).

Covers: sensible defaults, loading `.tst/config.yaml`, actionable
validation errors, network allowlist forms, the boundary wall being
steering-refused (the agent can't move its own wall), loop wiring of
writable_paths, and the daemon's `boundary_update` emission.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tstd.boundary_config import (
    BoundaryConfig,
    BoundarySection,
    load_workspace_boundary,
)
from tstd.config import ConfigError
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import BoundaryUpdate as BoundaryUpdateEvent
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import Tool, ToolDispatcher, ToolRegistry

# ── Defaults ────────────────────────────────────────────────────────────


class TestDefaults:
    def test_absent_config_returns_defaults(self, tmp_path: Path) -> None:
        cfg = load_workspace_boundary(tmp_path)
        assert cfg.boundary.writable_paths == ["**"]  # workspace-only writes
        assert cfg.boundary.allowed_commands == []
        assert cfg.boundary.network == "deny"  # no network
        assert cfg.caps.spend_usd == 25.0  # conservative spend cap
        assert cfg.caps.wall_clock_hours == 8.0
        assert cfg.caps.max_iterations == 200
        assert cfg.allowed_hosts == frozenset()  # deny → no hosts


# ── Loading and validation ──────────────────────────────────────────────


class TestLoad:
    def _write(self, tmp_path: Path, yaml_text: str) -> Path:
        cfg_path = tmp_path / ".tst" / "config.yaml"
        cfg_path.parent.mkdir(exist_ok=True)
        cfg_path.write_text(yaml_text)
        return cfg_path

    def test_loads_valid_config(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "boundary:\n"
            "  writable_paths: [src/**, tests/**]\n"
            "  allowed_commands: [cargo, git]\n"
            "  network: [api.example.com]\n"
            "caps:\n"
            "  spend_usd: 10.0\n"
            "  wall_clock_hours: 4.0\n"
            "  max_iterations: 50\n",
        )
        cfg = load_workspace_boundary(tmp_path)
        assert cfg.boundary.writable_paths == ["src/**", "tests/**"]
        assert cfg.boundary.allowed_commands == ["cargo", "git"]
        assert cfg.allowed_hosts == frozenset({"api.example.com"})
        assert cfg.caps.spend_usd == 10.0
        assert cfg.caps.max_iterations == 50

    def test_partial_config_uses_defaults_for_missing_keys(self, tmp_path: Path) -> None:
        self._write(tmp_path, "caps:\n  spend_usd: 5.0\n")
        cfg = load_workspace_boundary(tmp_path)
        assert cfg.caps.spend_usd == 5.0
        assert cfg.boundary.writable_paths == ["**"]  # default applied
        assert cfg.boundary.network == "deny"

    def test_invalid_network_value_names_the_key(self, tmp_path: Path) -> None:
        self._write(tmp_path, "boundary:\n  network: maybe\n")
        with pytest.raises(ConfigError) as ei:
            load_workspace_boundary(tmp_path)
        assert "network" in str(ei.value)

    def test_negative_spend_named(self, tmp_path: Path) -> None:
        self._write(tmp_path, "caps:\n  spend_usd: -5\n")
        with pytest.raises(ConfigError) as ei:
            load_workspace_boundary(tmp_path)
        assert "spend_usd" in str(ei.value)

    def test_empty_writable_entry_named(self, tmp_path: Path) -> None:
        self._write(tmp_path, "boundary:\n  writable_paths: ['']\n")
        with pytest.raises(ConfigError) as ei:
            load_workspace_boundary(tmp_path)
        assert "writable_paths" in str(ei.value)

    def test_non_mapping_yaml_rejected(self, tmp_path: Path) -> None:
        self._write(tmp_path, "- just\n- a\n- list\n")
        with pytest.raises(ConfigError):
            load_workspace_boundary(tmp_path)

    def test_invalid_yaml_rejected(self, tmp_path: Path) -> None:
        self._write(tmp_path, "boundary: [unclosed\n")
        with pytest.raises(ConfigError):
            load_workspace_boundary(tmp_path)


# ── Loop wiring: writable_paths feed the guard ──────────────────────────


async def _stub_worker(prompt: str) -> str:
    return "B"


class TestLoopWiring:
    async def test_session_boundary_config_drives_guard(self, tmp_path: Path) -> None:
        ws = tmp_path
        session = Session(str(ws))
        session.boundary_config = BoundaryConfig(
            boundary=BoundarySection(writable_paths=["src/**"])
        )
        router = TierRouter(lead_turns=3)
        config = make_config()

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="fs_edit",
                description="test edit tool",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                side_effect_class="auto",
                parallel_safe=False,
                path_fields=("path",),
                mutates=True,
            )
        )
        dispatcher = ToolDispatcher(registry)

        async def handler(session: object, path: str) -> str:
            return "ran"

        dispatcher.register_handler("fs_edit", handler)

        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="fs_edit",
                        tool_arguments=f'{{"path": "{ws / "README.md"}"}}',
                    ),
                    Script(kind="stream", content="Done"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, registry, dispatcher)
        await session.add_user_message("write README")
        await wait_for_turn(session, 1)

        # The loop built the boundary from session.boundary_config, so the
        # write to README.md (outside src/**) is refused as C.
        events = [
            e
            for e in session.event_log.all_events
            if isinstance(e, ToolCallEvent) and e.name == "fs_edit"
        ]
        assert events and events[0].decision_class == "C"
        results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
        assert results and results[0].status == "error"
        assert "Refused" in results[0].output

        await runner.cancel()


# ── Daemon emits boundary_update on open ────────────────────────────────


class TestDaemonBoundaryUpdate:
    async def test_open_workspace_emits_boundary_update(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = json.dumps({"type": "open_workspace", "path": str(tmp_path)})
        response = await daemon._handle_message(raw, None)
        assert response is not None
        sid = json.loads(response)["session_id"]

        sess = daemon.session_registry.get(sid)
        assert sess is not None
        assert sess.boundary_config.boundary.writable_paths == ["**"]  # defaults

        updates = [e for e in sess.event_log.all_events if isinstance(e, BoundaryUpdateEvent)]
        assert len(updates) == 1
        assert updates[0].network == "deny"
        assert updates[0].source == "defaults"

        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()
