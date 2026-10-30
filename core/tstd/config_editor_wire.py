"""Wire shapes of Settings → Configuration's schema (TD-5203).

Plain models, imported by :mod:`tstd.protocol` for the ``config_editor``
event and by :mod:`tstd.config_schema`, which fills them from the config
models. Every pydantic model of the config becomes one ``ConfigModelDef``;
a field that holds a model refers to it by name (``ref``), so a recursive
model (a tier's ``fallbacks`` are tiers) stays a finite schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

ConfigKind = Literal[
    "bool",
    "int",
    "float",
    "str",
    "enum",
    "list",
    "map",
    "object",
    "json",
    "union",
    "secrets",
]

#: One step of a key path: a mapping key, or a list position.
ConfigPathPart = str | int


class ConfigNode(BaseModel):
    """What one value is, and what may be done to it.

    ``kind`` picks the control. ``map`` and ``list`` hold ``entries`` and
    ``items``; ``object`` names its model in ``ref``; ``union`` lists its
    ``variants``. ``secrets`` is a name → value map whose values never leave
    the daemon: the event carries where each lives instead. ``default`` is
    the built-in value when ``has_default``; a key at a fixed place in the
    file gets its reset value from the event's ``defaults`` instead.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ConfigKind
    ref: str | None = None
    nullable: bool = False
    title: str = ""
    description: str = ""
    unit: str = ""
    default: JsonValue = None
    has_default: bool = False
    #: False: shown, never written here; ``note`` says where it is edited.
    editable: bool = True
    resettable: bool = True
    multiline: bool = False
    note: str = ""
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    exclusive_maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    choices: list[JsonValue] = Field(default_factory=list)
    #: The keys of the map at this path are the choices (a preset, a key).
    choices_from: list[str] = Field(default_factory=list)
    items: ConfigNode | None = None
    entries: ConfigNode | None = None
    variants: list[ConfigNode] = Field(default_factory=list)
    #: For ``map`` and ``list``: whether entries are added or removed here.
    addable: bool = True
    removable: bool = True
    add_note: str = ""
    remove_note: str = ""


class ConfigField(BaseModel):
    """One key of a model, in the model's own order."""

    model_config = ConfigDict(extra="forbid")

    name: str
    required: bool = False
    node: ConfigNode


class ConfigModelDef(BaseModel):
    """A config model: its fields, and what a new entry's form asks for."""

    model_config = ConfigDict(extra="forbid")

    name: str
    fields: list[ConfigField] = Field(default_factory=list)
    #: Fields the add form shows for a new entry of this model.
    add_fields: list[str] = Field(default_factory=list)
    #: True when ``{}`` is a valid instance, so a new one needs no form.
    empty_ok: bool = False


class ConfigEdit(BaseModel):
    """The edit a ``config_editor`` event acknowledges."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["set", "reset"]
    path: list[ConfigPathPart]


ConfigNode.model_rebuild()
