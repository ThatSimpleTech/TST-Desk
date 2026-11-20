"""Copy, relabel and remove catalog entries (TD-5237).

The writers edit a file a person also edits, so every case checks two
things: the catalog the daemon would load afterwards, and that the lines
the edit does not own are still there byte for byte.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.catalog_helpers import catalog_config_yaml
from tests.test_add_model import _ask, _start, _stop
from tstd.config import ConfigError, default_config_yaml, load_config
from tstd.mock import MockProvider, Script
from tstd.peer_policy import LOOPBACK_VERBS, MACHINE_PEER_VERBS
from tstd.preset_write import delete_preset, duplicate_preset, save_preset_label

ANCHORED = """\
# my models
presets:
  shared:
    label: Shared
    description: One model on every role
    brain: &one
      slug: vendor/model-a
      base_url: http://127.0.0.1:8000/v1
      context_window: 32768
      max_output_tokens: 4096
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    worker: *one
    validator: *one
  borrower:
    label: Borrower
    brain: *one
    worker: *one
    validator: *one

# which one new chats use
active_preset: shared
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _catalog(tmp_path: Path) -> Path:
    return _write(tmp_path, catalog_config_yaml())


class TestDuplicate:
    def test_the_copy_is_the_same_model_under_a_new_label(self, tmp_path: Path) -> None:
        path = _catalog(tmp_path)
        duplicate_preset("local", "local-2", "Local, small window", path)
        cfg = load_config(path)
        copy, source = cfg.presets["local-2"], cfg.presets["local"]
        assert copy.label == "Local, small window"
        for tier in ("brain", "worker", "validator"):
            assert getattr(copy, tier) == getattr(source, tier)
        assert list(cfg.presets)[-1] == "local-2"

    def test_only_the_new_block_is_added(self, tmp_path: Path) -> None:
        before = catalog_config_yaml()
        path = _write(tmp_path, before)
        duplicate_preset("local", "local-2", "Copy", path)
        after = path.read_text(encoding="utf-8").split("\n")
        start = after.index("  local-2:")
        # The block is every following line indented under the entry.
        end = next(i for i in range(start + 1, len(after)) if not after[i].startswith("    "))
        assert "\n".join(after[:start] + after[end:]) == before

    def test_a_copy_shares_no_anchor_with_its_source(self, tmp_path: Path) -> None:
        path = _write(tmp_path, ANCHORED)
        duplicate_preset("shared", "mine", "Mine", path)
        text = path.read_text(encoding="utf-8")
        block = text[text.index("  mine:") : text.index("# which one")]
        assert "&" not in block and "*" not in block
        assert block.count("slug: vendor/model-a") == 3
        cfg = load_config(path)
        assert cfg.presets["mine"].brain == cfg.presets["shared"].brain
        assert cfg.presets["mine"].description == "One model on every role"
        # The comment that opens the next section stayed with it.
        assert "\n# which one new chats use\nactive_preset: shared\n" in text

    def test_a_label_cannot_inject_yaml(self, tmp_path: Path) -> None:
        path = _catalog(tmp_path)
        duplicate_preset("local", "odd", 'x"\n    picker: more', path)
        cfg = load_config(path)
        assert cfg.presets["odd"].label == 'x"\n    picker: more'
        assert cfg.presets["odd"].picker == cfg.presets["local"].picker

    @pytest.mark.parametrize(
        ("source", "name", "label", "says"),
        [
            ("nope", "copy", "Copy", "Unknown preset"),
            ("local", "vllm", "Copy", "already exists"),
            ("local", "Bad Name", "Copy", "lowercase slug"),
            ("local", "copy", "   ", "Give the model a name"),
            ("local", "copy", "x" * 41, "at most 40"),
        ],
    )
    def test_refusals_write_nothing(
        self, tmp_path: Path, source: str, name: str, label: str, says: str
    ) -> None:
        path = _catalog(tmp_path)
        with pytest.raises(ConfigError, match=says):
            duplicate_preset(source, name, label, path)
        assert path.read_text(encoding="utf-8") == catalog_config_yaml()


class TestLabel:
    def test_only_the_label_line_changes(self, tmp_path: Path) -> None:
        path = _write(tmp_path, ANCHORED)
        save_preset_label("shared", "House model", path=path)
        assert path.read_text(encoding="utf-8") == ANCHORED.replace(
            "    label: Shared", '    label: "House model"'
        )
        cfg = load_config(path)
        assert cfg.presets["shared"].label == "House model"
        assert cfg.presets["shared"].description == "One model on every role"
        assert set(cfg.presets) == {"shared", "borrower"}

    def test_a_missing_label_is_added_under_the_entry(self, tmp_path: Path) -> None:
        path = _write(tmp_path, ANCHORED.replace("    label: Borrower\n", ""))
        save_preset_label("borrower", "Second", "Same model again", path)
        text = path.read_text(encoding="utf-8")
        assert '  borrower:\n    label: "Second"\n    description: "Same model again"\n' in text
        cfg = load_config(path)
        assert cfg.presets["borrower"].label == "Second"
        assert cfg.presets["borrower"].description == "Same model again"

    def test_an_empty_description_removes_the_line(self, tmp_path: Path) -> None:
        path = _write(tmp_path, ANCHORED)
        save_preset_label("shared", "Shared", "", path)
        assert "description" not in path.read_text(encoding="utf-8")
        assert load_config(path).presets["shared"].description is None

    def test_a_label_cannot_inject_yaml(self, tmp_path: Path) -> None:
        path = _write(tmp_path, ANCHORED)
        save_preset_label("shared", 'a"\n    picker: more', path=path)
        cfg = load_config(path)
        assert cfg.presets["shared"].label == 'a"\n    picker: more'
        assert cfg.presets["shared"].picker == "primary"

    @pytest.mark.parametrize(
        ("name", "label", "description", "says"),
        [
            ("nope", "X", None, "Unknown preset"),
            ("shared", " ", None, "Give the model a name"),
            ("shared", "x" * 41, None, "at most 40"),
            ("shared", "X", "d" * 121, "at most 120"),
        ],
    )
    def test_refusals_write_nothing(
        self, tmp_path: Path, name: str, label: str, description: str | None, says: str
    ) -> None:
        path = _write(tmp_path, ANCHORED)
        with pytest.raises(ConfigError, match=says):
            save_preset_label(name, label, description, path)
        assert path.read_text(encoding="utf-8") == ANCHORED


class TestDelete:
    def test_the_entry_goes_and_the_rest_stays(self, tmp_path: Path) -> None:
        path = _write(tmp_path, ANCHORED)
        delete_preset("borrower", path)
        gone = (
            "  borrower:\n    label: Borrower\n    brain: *one\n"
            "    worker: *one\n    validator: *one\n"
        )
        assert path.read_text(encoding="utf-8") == ANCHORED.replace(gone, "")
        cfg = load_config(path)
        assert list(cfg.presets) == ["shared"]
        assert cfg.active_preset == "shared"

    def test_removing_the_active_entry_moves_active_to_the_first_left(self, tmp_path: Path) -> None:
        path = _catalog(tmp_path)
        before = load_config(path)
        names = list(before.presets)
        victim = names[0]
        text = path.read_text(encoding="utf-8")
        if "active_preset:" not in text:
            path.write_text(text + f"\nactive_preset: {victim}\n", encoding="utf-8")
        else:
            from tstd.config_write import save_active_preset

            save_active_preset(victim, path)
        delete_preset(victim, path)
        cfg = load_config(path)
        assert victim not in cfg.presets
        assert cfg.active_preset == names[1]
        assert f"active_preset: {names[1]}" in path.read_text(encoding="utf-8")

    def test_removing_the_last_entry_leaves_an_empty_catalog(self, tmp_path: Path) -> None:
        path = _write(tmp_path, default_config_yaml())
        from tests.test_add_model import _add

        _add(path)
        from tstd.config_write import save_active_preset

        save_active_preset("my-model", path)
        delete_preset("my-model", path)
        cfg = load_config(path)
        assert cfg.presets == {}
        assert cfg.active_preset == ""
        assert cfg.has_model() is False
        assert "\npresets: {}\n" in path.read_text(encoding="utf-8")

    def test_an_entry_another_one_borrows_from_is_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path, ANCHORED)
        with pytest.raises(ConfigError, match="reuses part of it"):
            delete_preset("shared", path)
        assert path.read_text(encoding="utf-8") == ANCHORED

    def test_the_computer_use_helper_model_is_refused(self, tmp_path: Path) -> None:
        text = catalog_config_yaml().replace(
            'local_worker_preset: ""', "local_worker_preset: vllm", 1
        )
        assert "local_worker_preset: vllm" in text
        path = _write(tmp_path, text)
        with pytest.raises(ConfigError, match="computer-use helper"):
            delete_preset("vllm", path)
        assert path.read_text(encoding="utf-8") == text

    def test_an_unknown_entry_is_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path, ANCHORED)
        with pytest.raises(ConfigError, match="Unknown preset"):
            delete_preset("nope", path)
        assert path.read_text(encoding="utf-8") == ANCHORED


def _mock() -> MockProvider:
    return MockProvider(default=Script(kind="text"))


class TestOverTheWire:
    """The three verbs as Settings sends them, against a running daemon."""

    async def test_duplicate_adds_an_entry_and_leaves_the_active_one(self, tmp_path: Path) -> None:
        daemon, task, ws = await _start(tmp_path, _mock())
        try:
            before = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
            state = await _ask(
                ws,
                {"type": "duplicate_preset", "source": "local", "label": "Local, tight"},
                "setup_state",
            )
            # setup_state lists the catalog sorted by key.
            assert state["presets"] == sorted([*before["presets"], "local-tight"])
            assert state["preset_labels"]["local-tight"] == "Local, tight"
            assert state["active_preset"] == before["active_preset"]
            on_disk = load_config(daemon.config_path)
            assert on_disk.presets["local-tight"].brain == on_disk.presets["local"].brain
        finally:
            await _stop(task, ws)

    async def test_a_second_copy_with_the_same_label_gets_its_own_key(self, tmp_path: Path) -> None:
        _daemon, task, ws = await _start(tmp_path, _mock())
        try:
            msg = {"type": "duplicate_preset", "source": "local", "label": "Copy"}
            first = await _ask(ws, msg, "setup_state")
            second = await _ask(ws, msg, "setup_state")
            added = [n for n in second["presets"] if n not in first["presets"]]
            assert len(added) == 1 and added[0] != "copy"
            assert "copy" in first["presets"]
        finally:
            await _stop(task, ws)

    async def test_relabel_changes_what_the_picker_shows(self, tmp_path: Path) -> None:
        _daemon, task, ws = await _start(tmp_path, _mock())
        try:
            state = await _ask(
                ws,
                {
                    "type": "set_preset_label",
                    "name": "local",
                    "label": "On this computer",
                    "description": "Nothing leaves the machine",
                },
                "setup_state",
            )
            assert state["preset_labels"]["local"] == "On this computer"
            assert state["preset_descriptions"]["local"] == "Nothing leaves the machine"
            assert "local" in state["presets"]
        finally:
            await _stop(task, ws)

    async def test_delete_moves_active_and_an_open_chat_keeps_its_model(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        daemon, task, ws = await _start(tmp_path / "data", _mock())
        try:
            before = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
            victim = before["active_preset"]
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            sess = daemon.session_registry.get(opened["session_id"])
            assert sess is not None and sess.config is not None
            snapshot = sess.config
            slug = snapshot.tier("brain").slug

            state = await _ask(ws, {"type": "delete_preset", "name": victim}, "setup_state")
            assert victim not in state["presets"]
            # The first entry left in the file, the loader's own choice.
            left = list(load_config(daemon.config_path).presets)
            assert victim not in left
            assert state["active_preset"] == left[0]
            assert state["has_model"] is True
            # TD-1721: the open chat still calls the model it opened with.
            assert sess.config is snapshot
            assert sess.config.tier("brain").slug == slug
        finally:
            await _stop(task, ws)

    @pytest.mark.parametrize(
        "msg",
        [
            {"type": "duplicate_preset", "source": "nope", "label": "Copy"},
            {"type": "set_preset_label", "name": "nope", "label": "X"},
            {"type": "delete_preset", "name": "nope"},
        ],
    )
    async def test_an_unknown_entry_is_a_typed_error(
        self, tmp_path: Path, msg: dict[str, Any]
    ) -> None:
        daemon, task, ws = await _start(tmp_path, _mock())
        try:
            text = daemon.config_path.read_text(encoding="utf-8")
            reply = await _ask(ws, msg, "setup_state")
            assert reply["type"] == "error"
            assert reply["code"] == "bad_request"
            assert "Unknown preset" in reply["message"]
            assert daemon.config_path.read_text(encoding="utf-8") == text
        finally:
            await _stop(task, ws)

    async def test_no_key_is_written_by_a_copy(self, tmp_path: Path) -> None:
        daemon, task, ws = await _start(tmp_path, _mock())
        try:
            await _ask(
                ws,
                {"type": "duplicate_preset", "source": "tst-default", "label": "Hosted copy"},
                "setup_state",
            )
            copy = load_config(daemon.config_path).presets["hosted-copy"]
            dumped = json.dumps(copy.model_dump(mode="json"))
            assert "api_key" not in dumped and "sk-" not in dumped
            assert (
                copy.brain.credential
                == load_config(daemon.config_path).presets["tst-default"].brain.credential
            )
        finally:
            await _stop(task, ws)


def test_another_computer_may_send_none_of_them_and_only_this_one_may_delete() -> None:
    verbs = {"duplicate_preset", "set_preset_label", "delete_preset"}
    assert not verbs & MACHINE_PEER_VERBS
    assert "delete_preset" in LOOPBACK_VERBS
