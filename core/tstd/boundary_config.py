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

from .attachments import AttachmentLimits
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

    def shell_allowlist(self) -> tuple[str, ...] | None:
        """The shell tool's allowlist, or ``None`` for unrestricted (TD-606).

        Empty means *any* command, which is what the template above has always
        told users. Callers must not read ``allowed_commands`` directly:
        ``ShellPolicy`` spells unrestricted as ``None``, and ``tuple([])`` is
        ``()`` — present and empty — so passing the raw list refused every
        command in any workspace without a `.tst/config.yaml`, which is the
        default state. This is the one place that translation lives.

        Permissive is safe here because the allowlist narrows a path that is
        already guarded: no rule in ``RULE_TABLE`` matches on tool name, so a
        shell call falls through to the ambiguous classifier and defaults to
        class B — an approval the user answers (§2.6). The allowlist is a
        second wall, not the only one.
        """
        return tuple(self.allowed_commands) or None


class CapsSection(BaseModel):
    """Declared caps (spec §12.4 ``caps``); enforced by TD-707."""

    spend_usd: float = Field(default=DEFAULT_SPEND_USD, ge=0)
    wall_clock_hours: float = Field(default=DEFAULT_WALL_CLOCK_HOURS, ge=0)
    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1)


class BoundaryConfig(BaseModel):
    """Top-level workspace boundary configuration."""

    boundary: BoundarySection = Field(default_factory=BoundarySection)
    caps: CapsSection = Field(default_factory=CapsSection)
    # Attachment caps (TD-1709).  Its own section rather than a fourth key
    # under ``caps``: every cap there pauses a running agent and is re-read on
    # resume, while these refuse a client's message outright and never pause
    # anything.  Filing them together would break the one sentence that makes
    # ``caps`` legible.
    attachments: AttachmentLimits = Field(default_factory=AttachmentLimits)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Hosts the agent may reach; empty means no network (deny)."""
        net = self.boundary.network
        if isinstance(net, list):
            return frozenset(net)
        return frozenset()


def _config_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".tst" / "config.yaml"


# Written into freshly opened workspaces (TD-1103).  Every line is a
# comment: the template documents the knobs without pinning values, so a
# scaffolded file round-trips to the defaults and tracks them as they
# change in later versions.
DEFAULT_CONFIG_TEMPLATE = """\
# TST Desk workspace boundary (spec §12.4).
#
# Everything below is commented out — the values shown ARE the defaults,
# so this file changes nothing until you edit it.  Uncomment and tighten
# to move the wall in.

# boundary:
#   # Glob patterns (workspace-relative) the agent may write to.
#   writable_paths:
#     - "**"
#   # Command allowlist for the shell tool; empty means any command.
#   allowed_commands: []
#   # "deny" blocks all network — or allowlist hosts explicitly:
#   # network:
#   #   - api.anthropic.com
#   network: deny

# caps:
#   spend_usd: 25.0        # autonomy pauses past this spend
#   wall_clock_hours: 8.0  # autonomy pauses past this runtime
#   max_iterations: 200    # autonomy pauses past this many iterations

# attachments:
#   max_file_bytes: 256000   # per text file attached to a message
#   max_total_bytes: 512000  # per message, across all its attachments
#   max_count: 10            # files per message
"""


def scaffold_workspace_config(workspace: str | Path) -> Path | None:
    """Plant a commented ``.tst/config.yaml`` when the workspace has none.

    Returns the path written, or None when a config already exists —
    scaffolding never overwrites a user's file.
    """
    path = _config_path(workspace)
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return path


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

    # An empty or comment-only file (e.g. the scaffolded template) means
    # "all defaults", not an error.
    if data is None:
        return BoundaryConfig()

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
