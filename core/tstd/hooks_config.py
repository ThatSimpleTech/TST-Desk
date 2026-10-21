"""Hook configuration (TD-5134).

A hook is a command TST Desk runs at a point in a session's life. The
same ``hooks:`` block is read from the user's ``config.yaml`` and from a
workspace's ``.tst/config.yaml``; the workspace's applies only once the
user has trusted that file (TD-5113, which already counts ``hooks`` as
loosening). Only pydantic here, so ``config.py`` can import it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

HookEvent = Literal["session_start", "user_prompt", "before_tool", "after_tool", "stop"]
HOOK_EVENTS: tuple[HookEvent, ...] = (
    "session_start",
    "user_prompt",
    "before_tool",
    "after_tool",
    "stop",
)
TOOL_EVENTS: frozenset[str] = frozenset({"before_tool", "after_tool"})

DEFAULT_HOOK_TIMEOUT_SECONDS = 30.0
MAX_HOOK_TIMEOUT_SECONDS = 600.0
MAX_HOOKS_PER_EVENT = 20


class HookEntry(BaseModel):
    """One command to run for an event."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=4000)
    timeout_seconds: float = Field(
        default=DEFAULT_HOOK_TIMEOUT_SECONDS, gt=0, le=MAX_HOOK_TIMEOUT_SECONDS
    )
    # Tool-name globs (``shell``, ``fs_*``). Empty matches every tool.
    # Only ``before_tool`` and ``after_tool`` read it.
    tools: list[str] = Field(default_factory=list, max_length=50)
    name: str | None = Field(default=None, max_length=80)


class HooksConfig(BaseModel):
    """The ``hooks:`` block: a list of commands per event."""

    model_config = ConfigDict(extra="forbid")

    session_start: list[HookEntry] = Field(default_factory=list, max_length=MAX_HOOKS_PER_EVENT)
    user_prompt: list[HookEntry] = Field(default_factory=list, max_length=MAX_HOOKS_PER_EVENT)
    before_tool: list[HookEntry] = Field(default_factory=list, max_length=MAX_HOOKS_PER_EVENT)
    after_tool: list[HookEntry] = Field(default_factory=list, max_length=MAX_HOOKS_PER_EVENT)
    stop: list[HookEntry] = Field(default_factory=list, max_length=MAX_HOOKS_PER_EVENT)

    @model_validator(mode="after")
    def _tools_only_on_tool_events(self) -> HooksConfig:
        for event in HOOK_EVENTS:
            if event in TOOL_EVENTS:
                continue
            for i, entry in enumerate(getattr(self, event)):
                if entry.tools:
                    raise ValueError(
                        f"{event}[{i}].tools: only before_tool and after_tool match tools"
                    )
        return self

    def entries(self, event: HookEvent) -> list[HookEntry]:
        """The hooks configured for *event*, in order."""
        entries: list[HookEntry] = getattr(self, event)
        return entries


class HooksConfigError(ValueError):
    """A ``hooks:`` block that does not validate."""


def hooks_from_mapping(path: Path, data: dict[str, Any] | None) -> HooksConfig:
    """The ``hooks`` section of a config mapping; empty when absent.

    Raises:
        HooksConfigError: The section is present but invalid.
    """
    raw = None if data is None else data.get("hooks")
    if raw is None:
        return HooksConfig()
    try:
        return HooksConfig.model_validate(raw)
    except ValidationError as e:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'hooks'}: {err['msg']}" for err in e.errors()
        )
        raise HooksConfigError(f"Invalid hooks in {path}: {details}") from e


def load_workspace_hooks(workspace: str | Path) -> HooksConfig:
    """A workspace's ``hooks`` as written, ungated (library use and tests).

    The daemon never calls this: it reads hooks through the trust gate
    (:class:`tstd.hooks.HookRunner`), so an untrusted file adds none.

    Raises:
        HooksConfigError: The file or its ``hooks`` block is invalid.
    """
    import yaml

    from .boundary_config import workspace_config_path

    path = workspace_config_path(workspace)
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HooksConfig()
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise HooksConfigError(f"Failed to read {path}: {e}") from e
    if data is not None and not isinstance(data, dict):
        raise HooksConfigError(f"{path} must contain a YAML mapping at the top level")
    return hooks_from_mapping(path, data)
