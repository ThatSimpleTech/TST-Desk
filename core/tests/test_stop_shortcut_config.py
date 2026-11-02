"""The system-wide stop shortcut setting (TD-5224).

The app shell reads ``computer_use.stop_shortcut`` from ``config.yaml`` at
launch and registers it; these pin the config side so the shipped file, the
model and the docs agree.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tstd.config import ComputerUseConfig, ModelConfig

SHIPPED = Path(__file__).resolve().parents[1] / "tstd" / "config.yaml"
DOCS = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"
DEFAULT = "Ctrl+Alt+Super+Period"


def test_default_is_not_bare_cmd_period() -> None:
    assert ComputerUseConfig().stop_shortcut == DEFAULT
    assert ComputerUseConfig().stop_shortcut.lower() not in {"cmd+period", "super+period"}


def test_shipped_config_carries_the_default() -> None:
    shipped = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    assert shipped["computer_use"]["stop_shortcut"] == DEFAULT


def test_empty_turns_it_off_and_a_combo_round_trips() -> None:
    off = ModelConfig.model_validate({"computer_use": {"stop_shortcut": ""}})
    assert off.computer_use.stop_shortcut == ""
    custom = ModelConfig.model_validate({"computer_use": {"stop_shortcut": "Ctrl+Shift+F12"}})
    assert custom.computer_use.stop_shortcut == "Ctrl+Shift+F12"


def test_documented() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert f"| `stop_shortcut` | string | `{DEFAULT}` |" in text
