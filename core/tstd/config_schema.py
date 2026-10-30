"""Settings → Configuration's schema, read off the config models (TD-5203).

Every pydantic model reachable from :class:`~tstd.config.ModelConfig`
becomes a :class:`~tstd.config_editor_wire.ConfigModelDef`, its fields in
the model's own order, their types, bounds, literals and defaults taken
from the annotations and ``FieldInfo``. Words come from
:mod:`tstd.config_help`. Nothing here is hand-listed per key except those
words and the few rules there (secrets, read-only, references), so a key
added to a model reaches the editor without touching this file.
"""

from __future__ import annotations

import types
import typing
from functools import lru_cache
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined, to_jsonable_python

from .config import ModelConfig
from .config_editor_wire import ConfigField, ConfigModelDef, ConfigNode
from .config_help import (
    ADD_FIELDS,
    CHOICES_FROM,
    FIELD_HELP,
    MULTILINE_FIELDS,
    NOT_ADDABLE,
    NOT_REMOVABLE,
    NOT_RESETTABLE,
    READ_ONLY_FIELDS,
    SECRET_FIELDS,
    SECTION_TITLES,
)
from .shell_read_only import READ_ONLY_ENTRIES

ROOT_MODEL = ModelConfig.__name__

#: A list whose items may only be some fixed strings.
_ITEM_CHOICES: dict[str, tuple[str, ...]] = {"ShellReadOnlyConfig.commands": READ_ONLY_ENTRIES}

_BOUNDS = (
    ("ge", "minimum"),
    ("le", "maximum"),
    ("gt", "exclusive_minimum"),
    ("lt", "exclusive_maximum"),
    ("min_length", "min_length"),
    ("max_length", "max_length"),
)


class SchemaError(TypeError):
    """A config field has a type the editor has no control for."""


def _strip_annotated(annotation: Any) -> tuple[Any, list[Any]]:
    """The bare type and the constraint objects ``Annotated`` carried."""
    metadata: list[Any] = []
    while get_origin(annotation) is typing.Annotated:
        args = get_args(annotation)
        annotation = args[0]
        for meta in args[1:]:
            metadata.extend(meta.metadata if isinstance(meta, FieldInfo) else [meta])
    return annotation, metadata


def _apply_bounds(node: ConfigNode, metadata: list[Any]) -> None:
    for meta in metadata:
        for attr, target in _BOUNDS:
            value = getattr(meta, attr, None)
            if isinstance(value, int | float) and not isinstance(value, bool):
                setattr(node, target, value)


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _type_node(annotation: Any, defs: dict[str, ConfigModelDef]) -> ConfigNode:
    """The node for a bare annotation, registering any model it reaches."""
    annotation, metadata = _strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        nullable = len(args) < len(get_args(annotation))
        if len(args) == 1:
            node = _type_node(args[0], defs)
        else:
            node = ConfigNode(kind="union", variants=[_type_node(a, defs) for a in args])
        node.nullable = node.nullable or nullable
    elif origin is Literal:
        node = ConfigNode(kind="enum", choices=list(get_args(annotation)))
    elif origin is list:
        (item,) = get_args(annotation)
        node = ConfigNode(kind="list", items=_type_node(item, defs))
    elif origin is dict:
        _, value = get_args(annotation)
        if value is Any:
            node = ConfigNode(kind="json")
        else:
            node = ConfigNode(kind="map", entries=_type_node(value, defs))
    elif annotation is Any:
        node = ConfigNode(kind="json")
    elif annotation is bool:
        node = ConfigNode(kind="bool")
    elif annotation is int:
        node = ConfigNode(kind="int")
    elif annotation is float:
        node = ConfigNode(kind="float")
    elif annotation is str:
        node = ConfigNode(kind="str")
    elif _is_model(annotation):
        _register(annotation, defs)
        node = ConfigNode(kind="object", ref=annotation.__name__)
    else:
        raise SchemaError(f"no editor control for {annotation!r}")
    _apply_bounds(node, metadata)
    return node


def _default(info: FieldInfo) -> tuple[bool, Any]:
    if info.default is PydanticUndefined and info.default_factory is None:
        return False, None
    value = info.get_default(call_default_factory=True, validated_data={})
    return True, to_jsonable_python(value)


def _field_node(
    model: type[BaseModel], name: str, info: FieldInfo, defs: dict[str, ConfigModelDef]
) -> ConfigNode:
    key = f"{model.__name__}.{name}"
    node = _type_node(info.annotation, defs)
    _apply_bounds(node, list(info.metadata))
    description, unit = FIELD_HELP.get(key, (info.description or "", ""))
    node.description, node.unit = description, unit
    node.has_default, node.default = _default(info)
    if model is ModelConfig:
        node.title = SECTION_TITLES.get(name, "")
    if key in SECRET_FIELDS:
        node = node.model_copy(update={"kind": "secrets", "entries": None, "editable": False})
        node.note = SECRET_FIELDS[key]
    if key in READ_ONLY_FIELDS:
        node.editable = False
        node.note = READ_ONLY_FIELDS[key]
    node.choices_from = list(CHOICES_FROM.get(key, ()))
    node.multiline = key in MULTILINE_FIELDS
    node.resettable = key not in NOT_RESETTABLE
    if key in NOT_ADDABLE:
        node.addable, node.add_note = False, NOT_ADDABLE[key]
    if key in NOT_REMOVABLE:
        node.removable, node.remove_note = False, NOT_REMOVABLE[key]
    if key in _ITEM_CHOICES and node.items is not None:
        node.items = node.items.model_copy(
            update={"kind": "enum", "choices": list(_ITEM_CHOICES[key])}
        )
    return node


def _empty_ok(model: type[BaseModel]) -> bool:
    try:
        model.model_validate({})
    except ValidationError:
        return False
    return True


def _register(model: type[BaseModel], defs: dict[str, ConfigModelDef]) -> None:
    if model.__name__ in defs:
        return
    # Placed before its fields are read, so a model that holds itself
    # (a tier's fallbacks) refers to this entry instead of recursing.
    entry = ConfigModelDef(name=model.__name__, empty_ok=_empty_ok(model))
    defs[model.__name__] = entry
    for name, info in model.model_fields.items():
        entry.fields.append(
            ConfigField(
                name=name,
                required=info.is_required(),
                node=_field_node(model, name, info, defs),
            )
        )
    required = [f.name for f in entry.fields if f.required]
    entry.add_fields = list(ADD_FIELDS.get(model.__name__, required))


@lru_cache(maxsize=1)
def editor_defs() -> dict[str, ConfigModelDef]:
    """Every config model the editor shows, by name. ``ROOT_MODEL`` is the top."""
    defs: dict[str, ConfigModelDef] = {}
    _register(ModelConfig, defs)
    return defs
