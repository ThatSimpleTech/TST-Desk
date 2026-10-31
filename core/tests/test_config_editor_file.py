"""TD-5203: Settings → Configuration on config.yaml files people wrote by hand.

Line endings stay as they were, a section the loader fills from the
packaged file can lose an entry, and a file the splicer cannot edit safely
(a key listed twice, a merge, an anchor, a Unicode line separator) is
refused with a plain reason and left byte-identical, never half-edited.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tstd.config import ensure_user_config, load_config
from tstd.config_editor import apply_edit, build_view, version_of
from tstd.config_editor_checks import ConfigEditRefused

TIER = (
    '{base_url: "http://127.0.0.1:8000/v1", input_price: 0, output_price: 0, '
    "cache_read_price: 0, context_window: 32768, max_output_tokens: 4096}"
)
ANCHORED = f"""\
presets:
  fast:
    label: Fast
    brain: &fast_tier {TIER}
    worker: *fast_tier
    validator: *fast_tier
  team:
    label: Team
    brain: *fast_tier
    worker: *fast_tier
    validator: *fast_tier
active_preset: fast
"""


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = ensure_user_config(tmp_path / "config.yaml")
    load_config(path)
    return path


def _refused(path: Path, *args: Any, **kwargs: Any) -> ConfigEditRefused:
    before = path.read_bytes()
    with pytest.raises(ConfigEditRefused) as caught:
        apply_edit(path, *args, **kwargs)
    assert path.read_bytes() == before, "a refused edit changed the file"
    return caught.value


def _edit_text(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_windows_line_endings_stay_on_disk(config: Path) -> None:
    crlf = config.read_bytes().replace(b"\n", b"\r\n")
    config.write_bytes(crlf)
    assert build_view(config).version == version_of(crlf.decode("utf-8"))
    apply_edit(config, "set", ["workflow", "max_parallel"], 5)
    after = config.read_bytes()
    assert b"\n" not in after.replace(b"\r\n", b"")
    assert after.count(b"\r\n") == crlf.count(b"\r\n")
    assert load_config(config).workflow.max_parallel == 5


def test_a_filled_section_loses_the_entry_removed(tmp_path: Path) -> None:
    """An install from before local_servers shipped: the loader fills them in."""
    path = tmp_path / "config.yaml"
    path.write_text("presets: {}\n", encoding="utf-8")
    shown = [s["name"] for s in build_view(path).values["local_servers"]]
    assert shown == ["Ollama", "LM Studio", "vLLM", "llama.cpp"]
    version = version_of(path.read_text(encoding="utf-8"))
    assert apply_edit(path, "reset", ["local_servers", 2], version=version) is not None
    names = [s.name for s in load_config(path).local_servers]
    assert names == ["Ollama", "LM Studio", "llama.cpp"]


def test_a_section_listed_twice_is_refused_by_name(config: Path) -> None:
    config.write_text(config.read_text(encoding="utf-8") + "\nsearch:\n  max_results: 3\n")
    assert load_config(config).search.max_results == 3
    for args in (("set", ["search", "max_results"], 5), ("reset", ["search", "max_results"])):
        refused = _refused(config, *args)
        assert refused.code == "write_failed" and "lists search twice" in refused.message


def test_a_key_beside_a_merge_is_refused_not_raised(config: Path) -> None:
    _edit_text(config, "computer_use:\n", "computer_use:\n  <<: {max_prompt_images: 1}\n")
    refused = _refused(config, "set", ["computer_use", "browser"], "playwright")
    assert refused.code == "write_failed" and "merge" in refused.message
    assert ["computer_use"] in build_view(config).locked_paths


def test_a_unicode_line_separator_in_a_comment_blocks_the_edit(config: Path) -> None:
    _edit_text(config, "  timeout_seconds: 15\n", "  timeout_seconds: 20 # note\u2028\n")
    refused = _refused(config, "reset", ["search", "timeout_seconds"])
    assert refused.code == "write_failed" and "line separator" in refused.message


def test_anchored_keys_are_locked_and_a_broken_alias_is_refused_plainly(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(ANCHORED, encoding="utf-8")
    load_config(path)
    locked = build_view(path).locked_paths
    assert ["presets", "fast", "brain"] in locked and ["presets", "team", "worker"] in locked
    assert ["presets", "fast", "label"] not in locked
    window = ["presets", "team", "worker", "context_window"]
    assert "anchor" in _refused(path, "set", window, 65536).message
    # Removing the model that defines the anchor would leave the other's aliases
    # dangling; the parser's words about the would-be text never reach the window.
    refused = _refused(path, "reset", ["presets", "fast"])
    assert refused.code == "write_failed"
    assert refused.message == "this key could not be written in place; edit config.yaml by hand"
