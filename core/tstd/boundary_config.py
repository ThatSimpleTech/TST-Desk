"""Workspace boundary configuration (TD-706) — ``.tst/config.yaml``.

Defines the wall the agent may not cross: ``writable_paths``,
``allowed_commands``, ``network``, and the caps (``spend_usd``,
``wall_clock_hours``, ``max_iterations``, ``shell_memory_mb``).  Mirrors
the spec §12.4 charter shape.  Sensible defaults apply when the file is absent:
workspace-only writes, no network, a conservative spend cap.  Validation
produces actionable errors naming the offending key.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .attachments import AttachmentLimits
from .config import ConfigError
from .host_memory import MIN_SHELL_MEMORY_MB, shell_memory_cap_mb

# Defaults when `.tst/config.yaml` is absent: workspace-only writes, no
# network, a conservative spend cap, 8 wall-clock hours, 200 iterations.
DEFAULT_WRITABLE_PATHS: tuple[str, ...] = ("**",)
DEFAULT_SPEND_USD = 25.0
DEFAULT_WALL_CLOCK_HOURS = 8.0
DEFAULT_MAX_ITERATIONS = 200
DEFAULT_MEMORY_MAX_LINES = 200


class BoundarySection(BaseModel):
    """The workspace wall (spec §12.4 ``boundary``).

    Unknown keys are refused: a typo like ``writable_path`` must fail
    loudly rather than silently fall back to the loosest default — the
    same rule the charter's top level applies (TD-4001).
    """

    model_config = ConfigDict(extra="forbid")

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
    """Declared caps (spec §12.4 ``caps``); enforced by TD-707.

    Unknown keys are refused, like ``BoundarySection``: a misspelled cap
    would otherwise silently revert to its default.

    ``shell_memory_mb`` (TD-5116) is the one cap that does not pause the
    run: it bounds each shell command's memory, and a command over it is
    killed. ``None`` is half of RAM, worked out on the machine that runs
    the command, so a file written on a big box does not pin its number.
    """

    model_config = ConfigDict(extra="forbid")

    spend_usd: float = Field(default=DEFAULT_SPEND_USD, ge=0)
    wall_clock_hours: float = Field(default=DEFAULT_WALL_CLOCK_HOURS, ge=0)
    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1)
    shell_memory_mb: int | None = Field(default=None, ge=MIN_SHELL_MEMORY_MB)

    def shell_memory_cap(self) -> int | None:
        """MiB each shell command may use; None only when RAM is unknown."""
        return shell_memory_cap_mb(self.shell_memory_mb)


class MemorySection(BaseModel):
    """Memory-file line cap (spec §5, TD-2103). Not an autonomy pause."""

    max_lines: int = Field(default=DEFAULT_MEMORY_MAX_LINES, ge=1)


class WorktreeSection(BaseModel):
    """A worktree per session in this workspace (TD-5126).

    ``enabled: false`` keeps every session in the main checkout. ``setup``
    runs once in each new worktree (``npm ci``, say). A command a repo
    ships is loosening, so it runs only once the file is trusted (TD-5113).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    setup: str = Field(default="", max_length=4096)


DEFAULT_STOP_CHECK_RETRIES = 3
DEFAULT_STOP_CHECK_TIMEOUT = 300
MAX_STOP_CHECK_TIMEOUT = 3600
MAX_STOP_CHECK_RETRIES = 20
MAX_STOP_CHECKS = 10
MAX_STOP_CHECK_CHARS = 4000


class StopCheckSection(BaseModel):
    """The done-check for interactive turns (TD-5124).

    ``checks`` uses the charter's definition-of-done shape: a ``$`` item
    runs as a shell command through the dispatcher (classifier, policy,
    approval), anything else is a statement the worker judges GREEN/RED.
    A single string is one check. Empty means no check. ``max_retries``
    is how many times a failure goes back to the model in one turn, and
    ``timeout_secs`` how long a ``$`` command may run (the shell tool's
    own default of 30 s is too short for a test suite). Unknown keys are
    refused, like ``caps``.
    """

    model_config = ConfigDict(extra="forbid")

    checks: list[str] = Field(default_factory=list, max_length=MAX_STOP_CHECKS)
    max_retries: int = Field(default=DEFAULT_STOP_CHECK_RETRIES, ge=0, le=MAX_STOP_CHECK_RETRIES)
    timeout_secs: int = Field(default=DEFAULT_STOP_CHECK_TIMEOUT, ge=1, le=MAX_STOP_CHECK_TIMEOUT)

    @field_validator("checks", mode="before")
    @classmethod
    def _one_or_many(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v

    @field_validator("checks")
    @classmethod
    def _checks_are_text(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for item in v:
            text = item.strip()
            if not text or text == "$":
                raise ValueError("checks entries must be a $ command or a statement")
            if len(text) > MAX_STOP_CHECK_CHARS:
                raise ValueError(
                    f"checks entries must be at most {MAX_STOP_CHECK_CHARS} characters"
                )
            out.append(text)
        return out


class BoundaryConfig(BaseModel):
    """Top-level workspace boundary configuration."""

    boundary: BoundarySection = Field(default_factory=BoundarySection)
    caps: CapsSection = Field(default_factory=CapsSection)
    # Attachment caps (TD-1709).  Its own section rather than another key
    # under ``caps``: the run caps there pause a running agent and are re-read
    # on resume, while these refuse a client's message outright and never
    # pause anything.  ``caps.shell_memory_mb`` is the story's own name
    # (TD-5116); it bounds the machine, not the message.
    attachments: AttachmentLimits = Field(default_factory=AttachmentLimits)
    # Line cap on ``.tst/memory/**`` (TD-2103). Its own section: this refuses
    # a write, it does not pause a running agent the way ``caps`` does.
    memory: MemorySection = Field(default_factory=MemorySection)
    # A worktree per session (TD-5126). Not a wall: ``enabled`` is read even
    # from an untrusted file, ``setup`` only from a trusted one.
    worktree: WorktreeSection = Field(default_factory=WorktreeSection)
    # Done-check for interactive turns (TD-5124). Its own section: it runs
    # when the model ends a turn, and neither pauses nor refuses anything.
    stop_check: StopCheckSection = Field(default_factory=StopCheckSection)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Hosts the agent may reach; empty means no network (deny)."""
        net = self.boundary.network
        if isinstance(net, list):
            return frozenset(net)
        return frozenset()


def workspace_config_path(workspace: str | Path) -> Path:
    """``<workspace>/.tst/config.yaml``."""
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
#   shell_memory_mb: null  # MiB per shell command; null is half of RAM

# attachments:
#   max_file_bytes: 4000000  # per file attached to a message
#   max_total_bytes: 8000000 # per message, across all its attachments
#   max_count: 10            # files per message

# memory:
#   max_lines: 200   # a memory file at this cap is distilled, not appended

# worktree:
#   enabled: true    # false keeps sessions in this checkout (TD-5126)
#   setup: ""        # runs once in each new worktree, e.g. "npm ci"

# stop_check:
#   # Run when the model ends a turn; a failure goes back to it as a
#   # message. "$ ..." is a shell command (approval as usual), anything
#   # else a statement the worker model judges. Empty means no check.
#   checks: []
#   max_retries: 3   # failures sent back per turn before it gives up
#   timeout_secs: 300  # how long a "$" check may run
"""


def scaffold_workspace_config(workspace: str | Path) -> Path | None:
    """Plant a commented ``.tst/config.yaml`` when the workspace has none.

    Returns the path written, or None when a config already exists —
    scaffolding never overwrites a user's file.
    """
    path = workspace_config_path(workspace)
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
    path = workspace_config_path(workspace)
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
    if data is not None and not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return boundary_from_mapping(path, data)


def boundary_from_mapping(path: Path, data: dict[str, Any] | None) -> BoundaryConfig:
    """Validate an already-parsed config mapping; ``None`` is the defaults.

    Shared by :func:`load_workspace_boundary` and the trust gate (TD-5113),
    which parses the same bytes it hashed.

    Raises:
        ConfigError: Naming the offending key.
    """
    if data is None:
        return BoundaryConfig()
    try:
        return BoundaryConfig.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise ConfigError(f"Invalid boundary config in {path}: {loc}: {first['msg']}") from e


def configured_spend_from_mapping(data: Any) -> float | None:
    """The ``caps.spend_usd`` in a parsed ``.tst/config.yaml``, or None.

    A missing file (``None``), a comment-only scaffold, and a ``caps``
    section that omits ``spend_usd`` are unset. The built-in pause still
    uses ``DEFAULT_SPEND_USD``; this only says whether the user assigned
    one. Callers pass the trust gate's ``view.effective`` (TD-5113).
    """
    if not isinstance(data, dict):
        return None
    caps = data.get("caps")
    if not isinstance(caps, dict) or "spend_usd" not in caps:
        return None
    amount = caps["spend_usd"]
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    return float(amount)


def save_spend_cap(workspace: str | Path, amount: float | None) -> None:
    """Write or clear ``caps.spend_usd``. ``None`` removes the key.

    Other sections stay. An empty ``caps`` mapping is dropped. Atomic
    write, same as ``save_policy``.
    """
    if amount is not None and amount < 0:
        raise ConfigError("spend_usd must be >= 0")
    path = workspace_config_path(workspace)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {path}: {e}") from e
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level")
        existing = loaded

    caps = existing.get("caps")
    if not isinstance(caps, dict):
        caps = {}
    if amount is None:
        caps.pop("spend_usd", None)
    else:
        caps["spend_usd"] = amount
    if caps:
        existing["caps"] = caps
    else:
        existing.pop("caps", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def boundary_source(workspace: str | Path) -> str:
    """Describe where the boundary came from (for the UI/event)."""
    path = workspace_config_path(workspace)
    return str(path) if path.exists() else "defaults"
