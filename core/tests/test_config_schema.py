"""TD-5203: every config.yaml key reaches Settings → Configuration, with words.

The editor's schema is read off the config models, so these tests walk the
models on their own (annotations, not the editor's walker) and fail when a
field is missing, out of order, or has no description: a key added to a
model later cannot be forgotten by the editor.
"""

from __future__ import annotations

import json
import types
import typing
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from tstd.config import ModelConfig
from tstd.config_editor_wire import ConfigNode
from tstd.config_help import FIELD_HELP
from tstd.config_schema import ROOT_MODEL, editor_defs
from tstd.protocol import ConfigEditor, parse_daemon_event
from tstd.shell_read_only import READ_ONLY_ENTRIES


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    if get_origin(annotation) in (Union, types.UnionType, list, dict, typing.Annotated):
        for arg in get_args(annotation):
            found.extend(_models_in(arg))
    return found


def _reachable() -> dict[str, type[BaseModel]]:
    seen: dict[str, type[BaseModel]] = {}
    stack: list[type[BaseModel]] = [ModelConfig]
    while stack:
        model = stack.pop()
        if model.__name__ in seen:
            continue
        seen[model.__name__] = model
        for info in model.model_fields.values():
            stack.extend(_models_in(info.annotation))
    return seen


def _node(model: str, field: str) -> ConfigNode:
    for entry in editor_defs()[model].fields:
        if entry.name == field:
            return entry.node
    raise AssertionError(f"{model}.{field} is not in the schema")


def test_every_model_field_is_in_the_editor_schema_in_order() -> None:
    models = _reachable()
    defs = editor_defs()
    assert set(defs) == set(models)
    assert ROOT_MODEL == "ModelConfig"
    for name, model in models.items():
        assert [f.name for f in defs[name].fields] == list(model.model_fields), name
        for field, info in model.model_fields.items():
            entry = next(f for f in defs[name].fields if f.name == field)
            assert entry.required == info.is_required(), f"{name}.{field}"


def test_every_field_has_a_description_and_every_description_a_field() -> None:
    missing = [
        f"{name}.{field.name}"
        for name, entry in editor_defs().items()
        for field in entry.fields
        if not field.node.description.strip()
    ]
    assert not missing, f"add words to tstd/config_help.py for: {missing}"
    real = {f"{name}.{f}" for name, model in _reachable().items() for f in model.model_fields}
    assert set(FIELD_HELP) <= real, f"help for no field: {sorted(set(FIELD_HELP) - real)}"


def test_section_titles_name_every_top_level_key() -> None:
    assert all(f.node.title for f in editor_defs()[ROOT_MODEL].fields)


@pytest.mark.parametrize(
    ("model", "field", "expected"),
    [
        ("SearchConfig", "max_results", {"kind": "int", "minimum": 1, "maximum": 20}),
        ("SearchConfig", "timeout_seconds", {"kind": "float", "exclusive_minimum": 0}),
        ("CompactionConfig", "summary", {"kind": "enum", "choices": ["model", "extractive"]}),
        ("TierConfig", "total_timeout", {"kind": "float", "nullable": True, "unit": "seconds"}),
        ("TierConfig", "slug", {"kind": "str", "nullable": True, "min_length": 1}),
        ("TierConfig", "input_price", {"kind": "float", "unit": "USD per million tokens"}),
        ("TierConfig", "extra_body", {"kind": "json", "nullable": True}),
        ("TierConfig", "credential", {"kind": "str", "choices_from": ["credentials"]}),
        ("Preset", "fast", {"kind": "object", "ref": "FastConfig", "nullable": True}),
        ("ModelConfig", "credentials", {"kind": "map", "editable": False}),
        ("ModelConfig", "presets", {"kind": "map", "addable": False, "removable": True}),
        ("ModelConfig", "active_preset", {"resettable": False, "choices_from": ["presets"]}),
        ("McpConfig", "servers", {"kind": "map", "addable": False, "removable": False}),
        ("McpServerConfig", "env", {"kind": "secrets", "editable": False}),
        ("McpServerConfig", "headers", {"kind": "secrets", "editable": False}),
        ("HooksConfig", "stop", {"kind": "list", "max_length": 20}),
        ("CompactionConfig", "summary_prompt", {"multiline": True}),
        ("SessionConfig", "default_effort", {"kind": "enum", "default": "medium"}),
    ],
)
def test_kinds_and_bounds_follow_the_annotations(
    model: str, field: str, expected: dict[str, Any]
) -> None:
    node = _node(model, field).model_dump()
    assert {k: node[k] for k in expected} == expected


def test_containers_name_what_they_hold() -> None:
    fallbacks = _node("TierConfig", "fallbacks")
    assert fallbacks.items is not None and fallbacks.items.ref == "TierConfig"
    command = _node("EmbeddingsConfig", "command")
    assert [v.kind for v in command.variants] == ["str", "list"]
    tiers = _node("ConsultConfig", "tiers")
    assert tiers.items is not None and tiers.items.choices == ["brain", "worker", "validator"]
    commands = _node("ShellReadOnlyConfig", "commands")
    assert commands.items is not None and commands.items.choices == list(READ_ONLY_ENTRIES)
    presets = _node("ModelConfig", "presets")
    assert presets.entries is not None and presets.entries.ref == "Preset"


def test_new_entry_forms_and_empty_models() -> None:
    defs = editor_defs()
    assert defs["LocalServerConfig"].add_fields == ["name", "base_url"]
    assert "slug" in defs["TierConfig"].add_fields
    # Fast takes hook_url or extra_body; a priority tier is extra_body alone.
    assert {"hook_url", "extra_body"} <= set(defs["FastConfig"].add_fields)
    assert defs["ReplyGuardConfig"].empty_ok
    assert not defs["FastConfig"].empty_ok  # needs hook_url or extra_body


def test_defaults_are_the_built_in_values() -> None:
    assert _node("SearchConfig", "max_results").default == 8
    assert _node("CompactionConfig", "summary").default == "extractive"
    assert _node("TierConfig", "base_url").has_default is False


def test_the_schema_crosses_the_wire_intact() -> None:
    event = ConfigEditor(root=ROOT_MODEL, defs=editor_defs())
    back = parse_daemon_event(event.model_dump_json())
    assert isinstance(back, ConfigEditor)
    assert back.defs == editor_defs()
    assert json.loads(event.model_dump_json())["defs"]["TierConfig"]["name"] == "TierConfig"
