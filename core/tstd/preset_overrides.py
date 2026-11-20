"""Context settings one model carries with it (TD-5234).

A catalog entry already says which model each role uses and how big its
window is. The settings that go with a model's size and speed did not
travel with it: how compaction writes its summary, when stale tool results
are cleared, how many workers run at once, the effort a chat starts on.
They were global, so moving between a small local model and a large hosted
one meant editing them by hand each time.

``overrides`` on an entry sets any of them for chats on that entry. It is
a whitelist of six sections, each a partial of the global section with the
same limits. Nothing here can loosen what the agent may do: approvals, the
shell lists, MCP servers, hooks, remote attach, notifications, autonomy and
keys are not in it, so picking a model never changes the trust surface.

This module imports nothing from :mod:`tstd.config`, which imports it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .effort import EffortLevel

# Spelled out, not imported: tstd.config imports this module. A test pins
# every field here to the annotation and limits of the field it overrides.
_TierName = Literal["brain", "worker", "validator"]


class _Partial(BaseModel):
    """A section where every key is optional and an unknown key is an error."""

    model_config = ConfigDict(extra="forbid")

    def changes(self) -> dict[str, object]:
        """The keys this entry sets. A key left out, or null, keeps the global value."""
        return {
            key: value
            for key, value in self.model_dump(exclude_unset=True).items()
            if value is not None
        }


class CompactionOverride(_Partial):
    """``compaction`` for chats on this model. See ``CompactionConfig``."""

    summary: Literal["model", "extractive"] | None = None
    summary_prompt: str | None = None
    summary_max_tokens: int | None = Field(default=None, ge=64)
    summary_timeout_seconds: float | None = Field(default=None, gt=0)
    background_percent: int | None = Field(default=None, ge=0, le=99)
    reinject_files: int | None = Field(default=None, ge=0)
    reinject_tokens: int | None = Field(default=None, ge=0)


class ClearToolResultsOverride(_Partial):
    """``clear_tool_results`` for chats on this model."""

    enabled: bool | None = None
    keep_recent_tool_results: int | None = Field(default=None, ge=0)
    clear_at_least_tokens: int | None = Field(default=None, ge=0)
    exclude_tools: list[str] | None = None


class WorkflowOverride(_Partial):
    """``workflow`` for chats on this model: size it to this model's server."""

    max_parallel: int | None = Field(default=None, ge=1, le=16)
    max_tasks: int | None = Field(default=None, ge=1, le=32)


class ConsultOverride(_Partial):
    """``consult`` for chats on this model."""

    tiers: list[_TierName] | None = None
    max_per_turn: int | None = Field(default=None, ge=1, le=20)

    @field_validator("tiers")
    @classmethod
    def _dedupe_tiers(cls, value: list[_TierName] | None) -> list[_TierName] | None:
        return None if value is None else list(dict.fromkeys(value))


class SessionOverride(_Partial):
    """What a chat on this model starts with. The log and file caps stay global."""

    default_effort: EffortLevel | None = None
    default_ultracode: bool | None = None


class ComputerUseOverride(_Partial):
    """How many images this model's requests may keep. The rest stays global."""

    max_prompt_images: int | None = Field(default=None, ge=0)


#: The global sections an entry may override, in config.yaml's order.
OVERRIDE_SECTIONS: tuple[str, ...] = (
    "session",
    "clear_tool_results",
    "compaction",
    "consult",
    "workflow",
    "computer_use",
)


class PresetOverrides(_Partial):
    """The ``overrides`` block of one catalog entry."""

    session: SessionOverride | None = None
    clear_tool_results: ClearToolResultsOverride | None = None
    compaction: CompactionOverride | None = None
    consult: ConsultOverride | None = None
    workflow: WorkflowOverride | None = None
    computer_use: ComputerUseOverride | None = None

    def sections(self) -> dict[str, dict[str, object]]:
        """Each section this entry changes, with the keys it sets."""
        found: dict[str, dict[str, object]] = {}
        for name in OVERRIDE_SECTIONS:
            part: _Partial | None = getattr(self, name)
            if part is not None and part.changes():
                found[name] = part.changes()
        return found


def merged_sections(config: BaseModel, overrides: PresetOverrides | None) -> dict[str, BaseModel]:
    """The global sections of *config* with *overrides* laid over them.

    Only sections the entry changes are returned. A key the entry sets
    wins; every other key keeps *config*'s value; a list replaces the
    global list whole. Each result is built by the section's own model, so
    its rules apply to the merged value too. *config* is not changed.

    Raises:
        ValueError: A merged section is not valid. pydantic's message
            names the key.
    """
    if overrides is None:
        return {}
    merged: dict[str, BaseModel] = {}
    for name, changes in overrides.sections().items():
        base: BaseModel = getattr(config, name)
        merged[name] = type(base).model_validate({**base.model_dump(), **changes})
    return merged
