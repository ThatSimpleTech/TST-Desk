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
    scaffold_workspace_config,
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


# ── Scaffolding (TD-1103) ─────────────────────────────────────────────


class TestScaffold:
    def test_scaffold_plants_comment_only_template(self, tmp_path: Path) -> None:
        written = scaffold_workspace_config(tmp_path)
        assert written is not None
        assert written == tmp_path / ".tst" / "config.yaml"
        text = written.read_text(encoding="utf-8")
        assert text.startswith("#")
        # The template documents every default knob.
        for knob in ("writable_paths", "allowed_commands", "network", "spend_usd"):
            assert knob in text

    def test_scaffolded_template_round_trips_to_defaults(self, tmp_path: Path) -> None:
        path = scaffold_workspace_config(tmp_path)
        assert path is not None
        cfg = load_workspace_boundary(tmp_path)
        assert cfg == BoundaryConfig()  # commented template == defaults

    def test_scaffold_never_overwrites(self, tmp_path: Path) -> None:
        existing = tmp_path / ".tst" / "config.yaml"
        existing.parent.mkdir(parents=True)
        existing.write_text("caps:\n  spend_usd: 5.0\n", encoding="utf-8")
        assert scaffold_workspace_config(tmp_path) is None
        assert "5.0" in existing.read_text(encoding="utf-8")

    def test_empty_file_is_defaults_not_error(self, tmp_path: Path) -> None:
        # The scaffolded template must load: YAML-safe_load of a
        # comment-only (or empty) file yields None, so None means defaults,
        # not ConfigError.
        cfg_file = tmp_path / ".tst" / "config.yaml"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text("# nothing here yet\n", encoding="utf-8")
        assert load_workspace_boundary(tmp_path) == BoundaryConfig()


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
    async def test_session_boundary_config_drives_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path
        # Feed a workspace-relative path from inside the workspace: the
        # loop parses tool arguments as JSON (an absolute tmp_path has
        # backslashes on Windows — invalid JSON), and the guard refuses
        # drive-letter absolutes as windows_unsafe before any writable
        # logic runs (TD-1406).  Relative input pins the C refusal on
        # every platform.
        monkeypatch.chdir(ws)
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
                        tool_arguments=json.dumps({"path": "README.md"}),
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


# ── Workspace switching re-resolves steering (TD-1103) ─────────────────


class TestWorkspaceSwitch:
    async def test_switching_workspaces_re_resolves_steering(self, tmp_path: Path) -> None:
        """Each session's loop builds its own PromptAssembler from the
        session's workspace (loop.py), so opening workspace B after
        workspace A gets B's steering — never A's.  Pin the AC4 guarantee
        end-to-end: one turn per workspace against the mock provider, then
        inspect the system messages the two loops actually sent."""
        marker_a = "ALPHAWORKSPACE-MARKER"
        marker_b = "BETAWORKSPACE-MARKER"
        ws_a = tmp_path / "alpha"
        ws_b = tmp_path / "beta"
        (ws_a / "AGENTS.md").parent.mkdir(parents=True, exist_ok=True)
        (ws_a / "AGENTS.md").write_text(marker_a, encoding="utf-8")
        (ws_b / "AGENTS.md").parent.mkdir(parents=True, exist_ok=True)
        (ws_b / "AGENTS.md").write_text(marker_b, encoding="utf-8")

        async def run_turn(ws: Path) -> tuple[Session, MockProvider]:
            session = Session(str(ws))
            session.boundary_config = load_workspace_boundary(ws)
            mock = MockProvider(default=Script(kind="stream", content="ok"))
            runner = await start_loop(session, TierRouter(), mock, make_config())
            await session.add_user_message("hi")
            await wait_for_turn(session, 1)
            await runner.cancel()
            return session, mock

        _, mock_a = await run_turn(ws_a)
        _, mock_b = await run_turn(ws_b)

        sys_a = mock_a.calls[0].messages[0].content or ""
        sys_b = mock_b.calls[0].messages[0].content or ""
        assert marker_a in sys_a and marker_b not in sys_a
        assert marker_b in sys_b and marker_a not in sys_b


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
        # TD-1103: opening plants the commented template, so the source is
        # the scaffolded file — the values are still the defaults.
        assert updates[0].source == str(tmp_path / ".tst" / "config.yaml")
        assert (tmp_path / ".tst" / "config.yaml").exists()

        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()


class TestDaemonOpenWorkspaceValidation:
    async def test_missing_workspace_refused(self, tmp_path: Path) -> None:
        """open_workspace with a nonexistent path errors, creating nothing (TD-1103)."""
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = json.dumps({"type": "open_workspace", "path": str(tmp_path / "nope")})
        response = await daemon._handle_message(raw, None)
        assert response is not None
        err = json.loads(response)
        assert err["type"] == "error"
        assert err["code"] == "workspace_not_found"
        assert "nope" in err["message"]
        assert daemon.session_registry.count == 0
        await daemon._shutdown()

    async def test_file_path_refused(self, tmp_path: Path) -> None:
        """A path that exists but is a file is not a workspace (TD-1103)."""
        not_a_dir = tmp_path / "file.txt"
        not_a_dir.write_text("hi", encoding="utf-8")
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = json.dumps({"type": "open_workspace", "path": str(not_a_dir)})
        response = await daemon._handle_message(raw, None)
        assert response is not None
        assert json.loads(response)["code"] == "workspace_not_found"
        assert daemon.session_registry.count == 0
        await daemon._shutdown()

    async def test_open_plants_scaffolded_config(self, tmp_path: Path) -> None:
        """Opening a fresh workspace leaves a commented config behind (TD-1103)."""
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = json.dumps({"type": "open_workspace", "path": str(tmp_path)})
        response = await daemon._handle_message(raw, None)
        assert response is not None
        sid = json.loads(response)["session_id"]
        text = (tmp_path / ".tst" / "config.yaml").read_text(encoding="utf-8")
        assert text.startswith("#")
        # Second open on a user's edited config must not clobber it.
        user_cfg = tmp_path / ".tst" / "config.yaml"
        user_cfg.write_text("caps:\n  spend_usd: 1.0\n", encoding="utf-8")
        raw2 = json.dumps({"type": "open_workspace", "path": str(tmp_path)})
        await daemon._handle_message(raw2, None)
        assert "1.0" in user_cfg.read_text(encoding="utf-8")

        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()
