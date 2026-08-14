"""Workspace boundary configuration (TD-706) — ``.tst/config.yaml``.

Defines the wall the agent may not cross: ``writable_paths``,
``allowed_commands``, ``network``, and the caps (``spend_usd``,
``wall_clock_hours``, ``max_iterations``).  Mirrors the spec §12.4
charter shape.  Sensible defaults apply when the file is absent:
workspace-only writes, no network, a conservative spend cap.  Validation
produces actionable errors naming the offending key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import ConfigError

# Defaults when `.tst/config.yaml` is absent: workspace-only writes, no
# network, a conservative spend cap, 8 wall-clock hours, 200 iterations.
DEFAULT_WRITABLE_PATHS: tuple[str, ...] = ("**",)
DEFAULT_SPEND_USD = 25.0
DEFAULT_WALL_CLOCK_HOURS = 8.0
DEFAULT_MAX_ITERATIONS = 200


class BoundarySection(BaseModel):
    """The workspace wall (spec §12.4 ``boundary``)."""

    writable_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_WRITABLE_PATHS),
        description="Glob patterns (workspace-relative) the agent may write to",
    )
    allowed_commands: list[str] = Field(
        default_factory=list,
        description="Command allowlist for the shell tool (TD-605)",
    )
    network: str | list[str] = Field(
        default="deny",
        description="'deny' or an explicit allowlist of hosts",
    )

    @field_validator("writable_paths")
    @classmethod
    def _writable_paths_non_empty(cls, v: list[str]) -> list[str]:
        if any(not isinstance(p, str) or not p.strip() for p in v):
            raise ValueError("writable_paths entries must be non-empty glob strings")
        return v

    @field_validator("network")
    @classmethod
    def _network_shape(cls, v: str | list[str]) -> str | list[str]:
        if isinstance(v, str):
            if v != "deny":
                raise ValueError("network must be 'deny' or a list of allowed hosts")
            return v
        if isinstance(v, list) and all(isinstance(h, str) and h for h in v):
            return v
        raise ValueError("network must be 'deny' or a list of allowed hosts")


class CapsSection(BaseModel):
    """Declared caps (spec §12.4 ``caps``); enforced by TD-707."""

    spend_usd: float = Field(default=DEFAULT_SPEND_USD, ge=0)
    wall_clock_hours: float = Field(default=DEFAULT_WALL_CLOCK_HOURS, ge=0)
    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1)


class BoundaryConfig(BaseModel):
    """Top-level workspace boundary configuration."""

    boundary: BoundarySection = Field(default_factory=BoundarySection)
    caps: CapsSection = Field(default_factory=CapsSection)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Hosts the agent may reach; empty means no network (deny)."""
        net = self.boundary.network
        if isinstance(net, list):
            return frozenset(net)
        return frozenset()


def _config_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".tst" / "config.yaml"


def load_workspace_boundary(workspace: str | Path) -> BoundaryConfig:
    """Load the workspace boundary from ``.tst/config.yaml``.

    Returns the default boundary when the file is absent.

    Raises:
        ConfigError: If the file is invalid YAML, not a mapping, or fails
            validation.  The message names the offending key so the user
            can fix it directly.
    """
    path = _config_path(workspace)
    if not path.exists():
        return BoundaryConfig()

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"Failed to read {path}: {e}") from e

    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    try:
        return BoundaryConfig.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise ConfigError(f"Invalid boundary config in {path}: {loc}: {first['msg']}") from e


def boundary_source(workspace: str | Path) -> str:
    """Describe where the boundary came from (for the UI/event)."""
    path = _config_path(workspace)
    return str(path) if path.exists() else "defaults"
