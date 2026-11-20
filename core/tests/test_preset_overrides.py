"""Context settings one model carries with it (TD-5234).

``effective_config`` is what a chat on an entry runs with: the daemon's
config with that entry's ``overrides`` laid over the global sections. The
cases here are the merge rules, what load refuses, and that a chat really
gets them when it is opened on the entry, switched to it, or revived.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from tests.catalog_helpers import catalog_config_yaml
from tests.test_add_model import _ask, _start, _stop
from tstd.config import (
    ClearToolResultsConfig,
    CompactionConfig,
    ComputerUseConfig,
    ConfigError,
    ConsultConfig,
    ModelConfig,
    SessionConfig,
    WorkflowConfig,
    load_config,
)
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.preset_apply import effective_config
from tstd.preset_overrides import (
    OVERRIDE_SECTIONS,
    ClearToolResultsOverride,
    CompactionOverride,
    ComputerUseOverride,
    ConsultOverride,
    PresetOverrides,
    SessionOverride,
    WorkflowOverride,
)
from tstd.preset_write import duplicate_preset
from tstd.protocol import TierState
from tstd.session_store import SessionStore

PAIRS: list[tuple[type[BaseModel], type[BaseModel]]] = [
    (SessionOverride, SessionConfig),
    (ClearToolResultsOverride, ClearToolResultsConfig),
    (CompactionOverride, CompactionConfig),
    (ConsultOverride, ConsultConfig),
    (WorkflowOverride, WorkflowConfig),
    (ComputerUseOverride, ComputerUseConfig),
]

# What the entries below set. Every other key must keep its global value.
SMALL = """\
    overrides:
      session:
        default_effort: low
      compaction:
        summary: extractive
        background_percent: 50
      clear_tool_results:
        keep_recent_tool_results: 1
        exclude_tools: [fs_read]
      consult:
        tiers: []
      workflow:
        max_parallel: 1
      computer_use:
        max_prompt_images: 0
"""
BIG = """\
    overrides:
      session:
        default_effort: high
        default_ultracode: true
"""


def _with(text: str, entry: str, block: str) -> str:
    header = f"\n  {entry}:\n"
    assert text.count(header) == 1, entry
    return text.replace(header, header + block, 1)


def _catalog_text() -> str:
    text = _with(catalog_config_yaml(), "local", SMALL)
    return _with(text, "budget", BIG)


def _config(tmp_path: Path, text: str | None = None) -> ModelConfig:
    path = tmp_path / "config.yaml"
    path.write_text(_catalog_text() if text is None else text, encoding="utf-8")
    return load_config(path)


class TestTheWhitelist:
    def test_the_six_sections_are_the_whole_list(self) -> None:
        assert set(PresetOverrides.model_fields) == set(OVERRIDE_SECTIONS)
        assert set(OVERRIDE_SECTIONS) == {
            "session",
            "clear_tool_results",
            "compaction",
            "consult",
            "workflow",
            "computer_use",
        }
        # Each is a real global section of the same name.
        assert set(OVERRIDE_SECTIONS) <= set(ModelConfig.model_fields)

    @pytest.mark.parametrize(("override", "base"), PAIRS)
    def test_every_override_key_has_its_global_keys_type_and_limits(
        self, override: type[BaseModel], base: type[BaseModel]
    ) -> None:
        for name, info in override.model_fields.items():
            assert name in base.model_fields, f"{override.__name__}.{name} overrides nothing"
            ours, theirs = info, base.model_fields[name]
            assert ours.annotation == theirs.annotation | None, name
            assert repr(ours.metadata) == repr(theirs.metadata), name
            assert ours.default is None and not ours.is_required(), name

    def test_only_what_suits_a_model_is_overridable_in_the_partial_sections(self) -> None:
        assert set(SessionOverride.model_fields) == {"default_effort", "default_ultracode"}
        assert set(ComputerUseOverride.model_fields) == {"max_prompt_images"}
        # Whole sections: a key added to the global one must be decided here too.
        for override, base in PAIRS[1:5]:
            assert set(override.model_fields) == set(base.model_fields), base.__name__

    @pytest.mark.parametrize(
        "block",
        [
            "policy: {skip_all: true}",
            "shell: {read_only: {enabled: false}}",
            "mcp: {servers: {}}",
            "hooks: {after_write: []}",
            "remote: {bind: 100.64.0.1}",
            "autonomy: {runtime: none}",
            "notify: {}",
            "credentials: {}",
            "engine: {kind: grok}",
            "session: {log_max_events: 5}",
            "session: {send_file_max_bytes: 1}",
            "computer_use: {command: rm}",
            "computer_use: {local_worker_preset: vllm}",
            "workflow: {max_parralel: 2}",
        ],
    )
    def test_anything_else_is_an_error_at_load(self, tmp_path: Path, block: str) -> None:
        text = _with(catalog_config_yaml(), "local", f"    overrides:\n      {block}\n")
        with pytest.raises(ConfigError, match=r"presets\.local\.overrides"):
            _config(tmp_path, text)

    @pytest.mark.parametrize(
        "block",
        [
            "workflow: {max_parallel: 0}",
            "workflow: {max_parallel: 17}",
            "compaction: {background_percent: 100}",
            "compaction: {summary: clever}",
            "session: {default_effort: enormous}",
            "consult: {tiers: [manager]}",
            "computer_use: {max_prompt_images: -1}",
        ],
    )
    def test_a_value_outside_the_global_keys_limits_is_an_error_at_load(
        self, tmp_path: Path, block: str
    ) -> None:
        text = _with(catalog_config_yaml(), "local", f"    overrides:\n      {block}\n")
        with pytest.raises(ConfigError, match=r"presets\.local\.overrides"):
            _config(tmp_path, text)


class TestEffectiveConfig:
    def test_no_overrides_is_the_snapshot_a_session_took_before(self, tmp_path: Path) -> None:
        config = _config(tmp_path, catalog_config_yaml())
        for name in config.presets:
            assert effective_config(config, name) == config.model_copy(
                update={"active_preset": name}
            )

    def test_a_set_key_wins_and_every_other_key_keeps_its_global_value(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        seen = effective_config(config, "local")
        assert seen.active_preset == "local"
        assert seen.session.default_effort == "low"
        assert seen.compaction.summary == "extractive"
        assert seen.compaction.background_percent == 50
        assert seen.clear_tool_results.keep_recent_tool_results == 1
        assert seen.workflow.max_parallel == 1
        assert seen.computer_use.max_prompt_images == 0

        expected = {
            "session": {"default_effort": "low"},
            "compaction": {"summary": "extractive", "background_percent": 50},
            "clear_tool_results": {"keep_recent_tool_results": 1, "exclude_tools": ["fs_read"]},
            "consult": {"tiers": []},
            "workflow": {"max_parallel": 1},
            "computer_use": {"max_prompt_images": 0},
        }
        before, after = config.model_dump(), seen.model_dump()
        for section, keys in expected.items():
            for key, value in before[section].items():
                assert after[section][key] == keys.get(key, value), f"{section}.{key}"
        untouched = set(before) - set(expected) - {"active_preset"}
        for section in untouched:
            assert after[section] == before[section], section

    def test_a_list_replaces_the_global_list_whole(self, tmp_path: Path) -> None:
        text = _catalog_text().replace("  exclude_tools: []", "  exclude_tools: [web_fetch]", 1)
        config = _config(tmp_path, text)
        assert config.clear_tool_results.exclude_tools == ["web_fetch"]
        seen = effective_config(config, "local")
        assert seen.clear_tool_results.exclude_tools == ["fs_read"]
        assert seen.consult.tiers == [] and config.consult.tiers == ["worker"]

    def test_a_null_or_an_empty_section_keeps_the_global_value(self, tmp_path: Path) -> None:
        block = "    overrides:\n      workflow: {max_parallel: null}\n      compaction: {}\n"
        config = _config(tmp_path, _with(catalog_config_yaml(), "local", block))
        assert effective_config(config, "local") == config.model_copy(
            update={"active_preset": "local"}
        )

    def test_the_global_config_and_other_entries_are_not_changed(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        before = config.model_dump()
        effective_config(config, "local")
        effective_config(config, "budget")
        assert config.model_dump() == before
        other = effective_config(config, "vllm")
        assert other.workflow == config.workflow
        assert other.session == config.session

    def test_a_later_global_edit_shows_through_the_keys_the_entry_leaves(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        edited = config.model_copy(
            update={"workflow": WorkflowConfig(max_parallel=5, max_tasks=20)}
        )
        seen = effective_config(edited, "local")
        assert seen.workflow.max_parallel == 1  # the entry's
        assert seen.workflow.max_tasks == 20  # the new global

    def test_an_unknown_entry_has_no_overrides(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        assert effective_config(config, "nope") == config.model_copy(
            update={"active_preset": "nope"}
        )


def test_a_copy_carries_its_sources_overrides(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_catalog_text(), encoding="utf-8")
    duplicate_preset("local", "local-2", "Local two", path)
    config = load_config(path)
    assert config.presets["local-2"].overrides == config.presets["local"].overrides
    assert effective_config(config, "local-2").workflow.max_parallel == 1
    written = yaml.safe_load(path.read_text(encoding="utf-8"))["presets"]["local-2"]
    # Only the keys the source set, so the copy still follows the global rest.
    assert written["overrides"]["workflow"] == {"max_parallel": 1}


def _mock() -> MockProvider:
    return MockProvider(default=Script(kind="stream", content="done"))


async def _seeded(tmp_path: Path) -> tuple[Daemon, Any, Any, Path]:
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(_catalog_text(), encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    daemon, task, ws = await _start(data, _mock())
    return daemon, task, ws, workspace


def _last_tier_state(sess: Any) -> TierState:
    states = [e for e in sess.event_log.all_events if isinstance(e, TierState)]
    assert states
    return states[-1]


class TestAChatGetsThem:
    async def test_a_chat_opened_on_the_entry_starts_with_its_settings(
        self, tmp_path: Path
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path)
        try:
            await _ask(ws, {"type": "set_preset", "name": "local"}, "setup_state")
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            sess = daemon.session_registry.get(opened["session_id"])
            assert sess is not None and sess.config is not None and sess.router is not None
            assert sess.config.workflow.max_parallel == 1
            assert sess.config.clear_tool_results.keep_recent_tool_results == 1
            assert sess.router.effort == "low"
            # The daemon's own config is still the global one.
            assert daemon.config.workflow.max_parallel == 3
            assert daemon.config.session.default_effort == "medium"
        finally:
            await _stop(task, ws)

    async def test_switching_a_chat_switches_its_settings_and_its_effort(
        self, tmp_path: Path
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path)
        try:
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            sid = opened["session_id"]
            sess = daemon.session_registry.get(sid)
            assert sess is not None and sess.config is not None and sess.router is not None
            assert sess.preset == "tst-default"
            assert sess.router.effort == "medium" and sess.router.ultracode is False

            await _ask(
                ws,
                {"type": "set_session_preset", "session_id": sid, "name": "budget"},
                "session_list",
            )
            assert sess.router.effort == "high" and sess.router.ultracode is True
            state = _last_tier_state(sess)
            assert state.effort == "high" and state.ultracode is True

            await _ask(
                ws,
                {"type": "set_session_preset", "session_id": sid, "name": "local"},
                "session_list",
            )
            assert sess.config.workflow.max_parallel == 1
            assert sess.config.compaction.summary == "extractive"
            assert sess.router.effort == "low"
            # "local" names an effort but no Ultracode default: the chat's switch stays.
            assert sess.router.ultracode is True
        finally:
            await _stop(task, ws)

    async def test_an_entry_that_names_no_effort_leaves_the_chats_own(self, tmp_path: Path) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path)
        try:
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            sid = opened["session_id"]
            sess = daemon.session_registry.get(sid)
            assert sess is not None and sess.router is not None
            await ws.send(json.dumps({"type": "set_effort", "session_id": sid, "effort": "xhigh"}))
            await _ask(
                ws,
                {"type": "set_session_preset", "session_id": sid, "name": "vllm"},
                "session_list",
            )
            assert sess.preset == "vllm"
            assert sess.router.effort == "xhigh"
        finally:
            await _stop(task, ws)

    async def test_the_next_turn_runs_on_the_switched_settings(self, tmp_path: Path) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path)
        try:
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            sid = opened["session_id"]
            sess = daemon.session_registry.get(sid)
            assert sess is not None
            await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
            await _ask(
                ws,
                {"type": "set_session_preset", "session_id": sid, "name": "local"},
                "session_list",
            )
            done = await _ask(
                ws,
                {"type": "user_message", "session_id": sid, "content": "hello"},
                "turn_complete",
            )
            assert done["type"] == "turn_complete"
            # What the turn's tools were handed: the workflow tool reads its
            # limits here, and the loop took the same object for clearing.
            runtime = sess.delegate_runtime
            assert runtime is not None
            assert runtime.config is sess.config
            assert runtime.config.workflow.max_parallel == 1
            assert runtime.config.clear_tool_results.keep_recent_tool_results == 1
            assert runtime.config.computer_use.max_prompt_images == 0
        finally:
            await _stop(task, ws)

    async def test_a_settings_edit_does_not_rewrite_an_open_chat(self, tmp_path: Path) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path)
        try:
            await _ask(ws, {"type": "set_preset", "name": "local"}, "setup_state")
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            sess = daemon.session_registry.get(opened["session_id"])
            assert sess is not None and sess.config is not None
            snapshot = sess.config
            await _ask(ws, {"type": "set_preset", "name": "budget"}, "setup_state")
            assert sess.config is snapshot
            assert sess.config.workflow.max_parallel == 1
        finally:
            await _stop(task, ws)


class TestRevive:
    async def _revived(self, tmp_path: Path, effort: str | None) -> Any:
        data = tmp_path / "data"
        workspace = tmp_path / "ws"
        data.mkdir()
        workspace.mkdir()
        (data / "config.yaml").write_text(_catalog_text(), encoding="utf-8")
        store = SessionStore(data)
        await store.upsert("sid-1", str(workspace), "running", preset="local")
        if effort is not None:
            await store.set_effort("sid-1", effort)
        from tstd.session_persist import SessionPersist

        SessionPersist(data).prepare("sid-1")
        daemon = Daemon(data_dir=data)
        await daemon._restore_sessions()
        return daemon

    async def _close(self, daemon: Daemon) -> None:
        for live in await daemon.session_registry.list_sessions():
            runner = daemon.session_registry.get_runner(live.id)
            if runner is not None:
                await runner.cancel()

    async def test_a_revived_chat_has_its_entrys_settings(self, tmp_path: Path) -> None:
        daemon = await self._revived(tmp_path, None)
        try:
            sess = daemon.session_registry.get("sid-1")
            assert sess is not None and sess.config is not None and sess.router is not None
            assert sess.preset == "local"
            assert sess.config.workflow.max_parallel == 1
            # No stored effort: it starts where its model says.
            assert sess.router.effort == "low"
        finally:
            await self._close(daemon)

    async def test_a_revived_chat_keeps_the_effort_it_had(self, tmp_path: Path) -> None:
        daemon = await self._revived(tmp_path, "xhigh")
        try:
            sess = daemon.session_registry.get("sid-1")
            assert sess is not None and sess.router is not None
            assert sess.router.effort == "xhigh"
        finally:
            await self._close(daemon)
