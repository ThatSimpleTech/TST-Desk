"""A catalog entry can name its engine, and a Grok entry needs no model (TD-5239).

Local, API and Grok are three entries of one catalog. A Grok entry is a
label and ``engine: grok``: the Grok CLI brings its own model, so the three
roles may be left out. These cases cover what load accepts, what the rest
of the code sees for an entry with no roles, which engine a chat gets, and
that a chat keeps the engine it started on.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.catalog_helpers import catalog_config_yaml
from tests.test_add_model import _ask, _start, _stop
from tstd.config import ConfigError, ModelConfig, load_config
from tstd.daemon import Daemon, _snapshot_slugs
from tstd.key_hosts import every_tier
from tstd.mock import MockProvider, Script
from tstd.model_cards import targets
from tstd.preset_apply import engine_refusal, no_model_refusal
from tstd.preset_write import delete_preset, duplicate_preset, save_preset_label
from tstd.scheduler.pin import ScheduledPin, pin_failure_summary, pinned_engine
from tstd.session import Session

GROK = """\
  grok:
    label: Grok
    description: The Grok CLI, with its own model
    engine: grok
"""
PRESETS_HEADER = "\npresets:\n"


def _text(extra: str = GROK, *, engine_kind: str | None = None) -> str:
    """The test catalog with *extra* entries first and ``tst-default`` active."""
    text = catalog_config_yaml()
    assert text.count(PRESETS_HEADER) == 1
    text = text.replace(PRESETS_HEADER, PRESETS_HEADER + extra, 1)
    # Named, or the loader would pick the first entry, which is the extra one.
    assert not re.search(r"^active_preset:", text, re.MULTILINE)
    text += "\nactive_preset: tst-default\n"
    if engine_kind is not None:
        shipped = "\nengine:\n  kind: native\n"
        assert text.count(shipped) == 1
        text = text.replace(shipped, f"\nengine:\n  kind: {engine_kind}\n", 1)
    return text


def _config(tmp_path: Path, text: str | None = None) -> ModelConfig:
    path = tmp_path / "config.yaml"
    path.write_text(_text() if text is None else text, encoding="utf-8")
    return load_config(path)


def _entry(body: str) -> str:
    return "  odd:\n" + body


TIER = """\
      base_url: http://127.0.0.1:8000/v1
      context_window: 32768
      max_output_tokens: 4096
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
"""


class TestWhatLoads:
    def test_a_grok_entry_needs_no_roles(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        grok = config.presets["grok"]
        assert grok.engine == "grok"
        assert grok.has_tiers is False and grok.tier_map() == {}
        assert grok.label == "Grok"

    def test_a_grok_entry_may_still_name_all_three(self, tmp_path: Path) -> None:
        body = f"    engine: grok\n    brain:\n{TIER}    worker:\n{TIER}    validator:\n{TIER}"
        config = _config(tmp_path, _text(_entry(body)))
        assert config.presets["odd"].has_tiers is True
        assert set(config.presets["odd"].tier_map()) == {"brain", "worker", "validator"}

    def test_a_catalog_from_before_loads_as_it_did(self, tmp_path: Path) -> None:
        config = _config(tmp_path, catalog_config_yaml())
        assert all(p.engine is None and p.has_tiers for p in config.presets.values())
        assert {config.preset_engine(name) for name in config.presets} == {"native"}

    @pytest.mark.parametrize(
        ("body", "says"),
        [
            ("    label: No model\n", "Only an entry with `engine: grok`"),
            ("    label: No model\n    engine: native\n", "Only an entry with `engine: grok`"),
            (
                f"    engine: grok\n    brain:\n{TIER}    worker:\n{TIER}",
                "name all three roles or none of them; missing: validator",
            ),
            (f"    brain:\n{TIER}", "missing: worker, validator"),
            (
                "    engine: grok\n    overrides:\n      workflow: {max_parallel: 1}\n",
                "`overrides` does nothing on an `engine: grok` entry",
            ),
            (
                "    engine: grok\n    fast:\n      extra_body: {priority: true}\n",
                "`fast` does nothing on an `engine: grok` entry",
            ),
            ("    engine: cursor\n", "presets.odd.engine"),
        ],
    )
    def test_what_is_refused(self, tmp_path: Path, body: str, says: str) -> None:
        with pytest.raises(ConfigError) as caught:
            _config(tmp_path, _text(_entry(body)))
        assert says in str(caught.value)
        assert "odd" in str(caught.value)


class TestWhatTheRestSees:
    def test_an_active_grok_entry_is_a_model_that_needs_no_key(self, tmp_path: Path) -> None:
        config = _config(tmp_path).model_copy(update={"active_preset": "grok"})
        assert config.has_model() is True
        assert config.requires_api_key() is False
        # Stand-ins, as with an empty catalog: code that reads a window still works.
        tiers = config.tiers()
        assert set(tiers) == {"brain", "worker", "validator"}
        assert all(t.slug is None and t.credential is None for t in tiers.values())

    def test_every_walk_over_the_catalog_skips_it(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        native = [name for name, p in config.presets.items() if p.has_tiers]
        assert len(native) == len(config.presets) - 1
        assert targets(config, "grok", None) == []
        assert targets(config, "grok", "brain") == []
        assert len(list(every_tier(config))) >= 3 * len(native)
        assert _snapshot_slugs(config)["grok"] == {}
        assert set(_snapshot_slugs(config)["local"]) == {"brain", "worker", "validator"}
        assert config.fallback_bindings("openrouter") == []

    def test_the_engine_is_the_entrys_then_the_global_one(self, tmp_path: Path) -> None:
        native_global = _config(tmp_path)
        assert native_global.preset_engine("grok") == "grok"
        assert native_global.preset_engine("local") == "native"
        assert native_global.preset_engine("nope") == "native"
        grok_global = _config(tmp_path, _text(engine_kind="grok"))
        assert grok_global.preset_engine("local") == "grok"
        pinned = _text(
            _entry(
                f"    engine: native\n    brain:\n{TIER}    worker:\n{TIER}    validator:\n{TIER}"
            ),
            engine_kind="grok",
        )
        assert _config(tmp_path, pinned).preset_engine("odd") == "native"

    def test_work_no_chat_owns_runs_on_an_entry_that_has_a_model(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        assert config.native_default() == config.active_preset
        on_grok = config.model_copy(update={"active_preset": "grok"})
        first_native = next(n for n, p in config.presets.items() if p.has_tiers)
        assert on_grok.native_default() == first_native
        only_grok = config.model_copy(
            update={"presets": {"grok": config.presets["grok"]}, "active_preset": "grok"}
        )
        assert only_grok.native_default() is None


class TestAChatKeepsItsEngine:
    def test_which_switches_are_refused(self, tmp_path: Path) -> None:
        body = f"    engine: native\n    brain:\n{TIER}    worker:\n{TIER}    validator:\n{TIER}"
        config = _config(tmp_path, _text(GROK + _entry(body)))
        # An entry that names no engine suits a chat on either, as before.
        assert engine_refusal(config, "local", "native") is None
        assert engine_refusal(config, "local", "grok") is None
        assert engine_refusal(config, "grok", "grok") is None
        assert engine_refusal(config, "odd", "native") is None
        assert engine_refusal(config, "nope", "native") is None
        to_grok = engine_refusal(config, "grok", "native")
        assert to_grok is not None and "no model of its own" in to_grok
        assert "Start a new chat" in to_grok
        to_native = engine_refusal(config, "odd", "grok")
        assert to_native is not None and "keeps the engine it started on" in to_native

    def test_only_the_native_loop_needs_the_entrys_model(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        assert no_model_refusal(config, "grok", "grok") is None
        assert no_model_refusal(config, "local", "native") is None
        assert no_model_refusal(config, "grok", "native") is not None


class TestScheduledPins:
    """TD-3812 stores a preset and an engine. The entry's engine fills an empty one."""

    @pytest.mark.parametrize(
        ("pin", "grok_available", "expected_engine", "summary"),
        [
            (ScheduledPin(preset="grok"), True, "grok", None),
            (ScheduledPin(preset="grok"), False, "grok", "grok engine is unavailable"),
            (ScheduledPin(preset="grok", engine="grok"), True, "grok", None),
            (
                ScheduledPin(preset="grok", engine="native"),
                True,
                "native",
                "preset 'grok' has no model of its own; it runs only on the grok engine",
            ),
            (ScheduledPin(preset="local"), False, None, None),
            (ScheduledPin(preset="local", engine="grok"), True, "grok", None),
            (
                ScheduledPin(preset="local", engine="grok"),
                False,
                "grok",
                "grok engine is unavailable",
            ),
            (ScheduledPin(), False, None, None),
            (
                ScheduledPin(preset="retired", engine="native"),
                False,
                "native",
                "preset 'retired' no longer exists",
            ),
        ],
    )
    def test_the_pin_table(
        self,
        tmp_path: Path,
        pin: ScheduledPin,
        grok_available: bool,
        expected_engine: str | None,
        summary: str | None,
    ) -> None:
        catalog = _config(tmp_path).presets
        assert pinned_engine(pin, catalog) == expected_engine
        assert pin_failure_summary(pin, catalog=catalog, grok_available=grok_available) == summary


class TestWriters:
    """TD-5237's three writers on an entry with no roles."""

    def test_copy_relabel_and_remove(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_text(), encoding="utf-8")
        duplicate_preset("grok", "grok-2", "Grok two", path)
        config = load_config(path)
        assert config.presets["grok-2"].engine == "grok"
        assert config.presets["grok-2"].has_tiers is False
        save_preset_label("grok", "Grok Build", path=path)
        assert load_config(path).presets["grok"].label == "Grok Build"
        delete_preset("grok", path)
        delete_preset("grok-2", path)
        # Both gone, and every other line as it was.
        assert path.read_text(encoding="utf-8") == _text(extra="")


async def _grok_stub(session: Session, **_kwargs: Any) -> None:
    """Stands in for the Grok loop: no CLI is started."""
    await asyncio.Event().wait()


def _mock() -> MockProvider:
    return MockProvider(default=Script(kind="stream", content="done"))


async def _seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str | None = None
) -> tuple[Daemon, Any, Any, Path]:
    monkeypatch.setattr("tstd.daemon.grok_loop", _grok_stub)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(_text() if text is None else text, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    daemon, task, ws = await _start(data, _mock())
    return daemon, task, ws, workspace


class TestOverTheWire:
    async def test_setup_state_marks_the_grok_entry_and_shows_no_roles_for_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _daemon, task, ws, _workspace = await _seeded(tmp_path, monkeypatch)
        try:
            before = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
            assert before["preset_engines"] == {"grok": "grok"}
            assert before["tier_slugs"] != {}
            state = await _ask(ws, {"type": "set_preset", "name": "grok"}, "setup_state")
            assert state["active_preset"] == "grok"
            assert state["has_model"] is True
            assert state["key_required"] is False
            assert state["tier_slugs"] == {}
            assert state["tier_cards"] == {}
            assert state["tier_credentials"] == {} and state["tier_loopback"] == {}
            # The Engine setting itself is not changed by picking an entry.
            assert state["engine"] == "native"
        finally:
            await _stop(task, ws)

    async def test_a_chat_opened_on_a_grok_entry_is_a_grok_chat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            native = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            assert native["engine"] == "native"
            await _ask(ws, {"type": "set_preset", "name": "grok"}, "setup_state")
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            assert opened["engine"] == "grok"
            sess = daemon.session_registry.get(opened["session_id"])
            assert sess is not None and sess.engine == "grok" and sess.preset == "grok"
            record = daemon._session_store.get(opened["session_id"])
            assert record is not None and record.engine == "grok" and record.preset == "grok"
            # The first chat is untouched.
            first = daemon.session_registry.get(native["session_id"])
            assert first is not None and first.engine == "native"
        finally:
            await _stop(task, ws)

    async def test_a_switch_across_engines_is_refused_and_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            sid = opened["session_id"]
            sess = daemon.session_registry.get(sid)
            assert sess is not None
            snapshot, was = sess.config, sess.preset
            ws_reply = await _ask(
                ws,
                {"type": "set_session_preset", "session_id": sid, "name": "grok"},
                "session_list",
            )
            assert ws_reply["type"] == "error"
            assert ws_reply["code"] == "engine_mismatch"
            assert "Start a new chat" in ws_reply["message"]
            assert sess.preset == was and sess.config is snapshot and sess.engine == "native"
            record = daemon._session_store.get(sid)
            assert record is not None and record.preset == was
            # A switch between two native entries still works.
            ok = await _ask(
                ws,
                {"type": "set_session_preset", "session_id": sid, "name": "local"},
                "session_list",
            )
            assert ok["type"] == "session_list" and sess.preset == "local"
        finally:
            await _stop(task, ws)

    async def test_the_native_loop_is_never_started_on_an_entry_with_no_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            before = len(await daemon.session_registry.list_sessions())
            raw = await daemon._start_session(str(workspace), preset="grok", engine="native")
            assert raw is not None
            reply = json.loads(raw)
            assert reply["type"] == "error" and reply["code"] == "engine_mismatch"
            assert len(await daemon.session_registry.list_sessions()) == before
            # A caller's engine still wins over an entry's when there is a model.
            raw = await daemon._start_session(str(workspace), preset="local", engine="grok")
            assert raw is not None and json.loads(raw)["engine"] == "grok"
        finally:
            await _stop(task, ws)

    async def test_the_doctor_skips_the_probe_for_an_active_grok_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, _workspace = await _seeded(tmp_path, monkeypatch)
        try:
            await _ask(ws, {"type": "set_preset", "name": "grok"}, "setup_state")
            rows = {row.name: row for row in await daemon._doctor_key_provider_rows()}
            assert rows["api_key"].status == "skip" and rows["provider"].status == "skip"
            assert "Grok CLI" in rows["provider"].detail
        finally:
            await _stop(task, ws)


class TestDistill:
    """Distill runs on TST Desk's own client whatever engine the chat used."""

    async def _run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
    ) -> list[tuple[str, str | None]]:
        seen: list[tuple[str, str | None]] = []

        async def fake_distill(session: Session, provider: Any, config: ModelConfig) -> None:
            seen.append((config.active_preset, config.tier("worker").slug))
            return None

        monkeypatch.setattr("tstd.daemon.distill_if_due", fake_distill)
        monkeypatch.setattr("tstd.daemon.completed_turn_count", lambda _s: 1)
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch, text)
        try:
            await _ask(ws, {"type": "set_preset", "name": "grok"}, "setup_state")
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            sess = daemon.session_registry.get(opened["session_id"])
            assert sess is not None and sess.engine == "grok"
            daemon._distilled_turns[sess.id] = 0  # LIVE2-08: a turn not yet distilled
            await daemon._run_distill(sess)
        finally:
            await _stop(task, ws)
        return seen

    async def test_with_grok_active_it_uses_a_model_on_this_computer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The manager's rule (LIVE2-08): never a hosted model unless the chat
        # used it, and Grok's own model is not an entry here.
        seen = await self._run(tmp_path, monkeypatch, _text())
        config = _config(tmp_path)
        local = config.local_default()
        assert local is not None
        worker = config.presets[local].worker
        assert worker is not None
        # Once: the quit finds nothing new since.
        assert seen == [(local, worker.slug)]

    async def test_with_no_model_anywhere_it_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tstd.config import default_config_yaml

        only_grok = default_config_yaml().replace("\npresets: {}\n", "\npresets:\n" + GROK, 1)
        assert "engine: grok" in only_grok
        assert await self._run(tmp_path, monkeypatch, only_grok) == []
