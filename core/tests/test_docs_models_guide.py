"""docs/models.md is the walkthrough the picker links to (TD-5172).

Its YAML examples must load through the real loader, it must name every
picker item a person can click, and the UI must link to it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tstd.config import load_config

ROOT = Path(__file__).resolve().parent.parent.parent
DOC = ROOT / "docs" / "models.md"

_BLOCK = re.compile(r"<!-- verify: model -->\n```yaml\n(.*?)\n```", re.S)


def _blocks() -> list[str]:
    return _BLOCK.findall(DOC.read_text(encoding="utf-8"))


def test_every_yaml_block_is_checked() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert text.count("```yaml") == len(_blocks()) >= 3


@pytest.mark.parametrize("index", range(3))
def test_examples_load(index: int, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_blocks()[index], encoding="utf-8")
    cfg = load_config(path)
    for name, preset in cfg.presets.items():
        assert preset.label, f"{name} in the guide has no label"
        assert preset.description, f"{name} in the guide has no description"


def test_names_every_picker_item() -> None:
    text = DOC.read_text(encoding="utf-8")
    for item in ("Effort", "More models", "Teams", "Add a model", "How models work", "Plan"):
        assert f"**{item}**" in text, item


def test_the_ui_links_here() -> None:
    links = (ROOT / "ui" / "src" / "lib" / "docs-links.ts").read_text(encoding="utf-8")
    assert "/models.md" in links
    for component in ("chat/ModelPicker.svelte", "SettingsModels.svelte"):
        body = (ROOT / "ui" / "src" / "lib" / "components" / component).read_text(encoding="utf-8")
        assert "MODELS_DOC_URL" in body, component
