"""Approval policy model (TD-801) — per-workspace rules in ``.tst/config.yaml``.

A policy maps ``(tool, argument pattern) → auto | ask | never``.  Rules are
globs over the tool name and a normalized *argument summary*; the most
specific matching rule wins.  When no rule matches, the effect derives from
the decision class (spec §12.2): A → ``auto``, B → ``ask``, C → the
configured ``class_c_default`` (``ask`` unless the workspace says ``never``).

The boundary always wins (prime directive §2 territory): dispatch refuses
boundary-crossing calls *before* policy is ever consulted (TD-602 ordering),
and this resolver adds a second layer — a class-C call never resolves to
``auto``, even when a rule says so.  Policy can tighten the wall, never
widen it.
"""

from __future__ import annotations

import fnmatch
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from .autonomy.classifier import DecisionClass
from .config import ConfigError
from .tools import Tool
from .tools.registry import SideEffectClass

# ── Models ──────────────────────────────────────────────────────────────

# Restrictiveness ranking for tie-breaking: never > ask > auto.
_RESTRICTIVENESS: dict[str, int] = {"never": 2, "ask": 1, "auto": 0}


class PolicyRule(BaseModel):
    """One ``(tool, argument pattern) → effect`` rule.

    Attributes:
        tool: Tool name or glob (``fs_*``) the rule applies to.
        args: Glob over the argument summary (see :func:`summarize_arguments`).
            ``"**"`` (the default) matches any call of the tool.
        effect: What the policy allows without asking.
    """

    tool: str
    args: str = "**"
    effect: SideEffectClass


class PolicyConfig(BaseModel):
    """The ``policy:`` section of ``.tst/config.yaml``."""

    rules: list[PolicyRule] = Field(default_factory=list)
    class_c_default: Literal["ask", "never"] = Field(
        default="ask",
        description="Effect for class-C calls with no matching rule: ask (default) or never",
    )


# ── Argument summaries ──────────────────────────────────────────────────


def summarize_arguments(
    tool: Tool, arguments: dict[str, Any], workspace: Path | None = None
) -> str:
    """Reduce a tool call to the string policy patterns match against.

    Metadata-driven, mirroring ``build_decision_request`` (TD-702) — never
    a guess over raw text:

    - a tool with ``path_fields`` → the first path, workspace-relative
      POSIX when a workspace is known (``src/app.py``), else as given
    - a tool with ``host_fields`` → the host
    - any tool with a string ``command`` argument → the command line
      (the shell tool's v0.1 convention, so ``npm test*`` rules read naturally)
    - anything else → canonical JSON (sorted keys, compact)
    """
    for field in tool.path_fields:
        raw = arguments.get(field)
        if isinstance(raw, str):
            if workspace is not None:
                try:
                    return Path(raw).resolve().relative_to(workspace.resolve()).as_posix()
                except (OSError, ValueError):
                    pass  # outside the workspace — match against the absolute path
            return Path(raw).as_posix()
    for field in tool.host_fields:
        raw = arguments.get(field)
        if isinstance(raw, str):
            return raw
    command = arguments.get("command")
    if isinstance(command, str):
        return command
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


# ── Resolution ──────────────────────────────────────────────────────────


def _specificity(rule: PolicyRule) -> tuple[int, int, int]:
    """Sort key: exact tool beats glob, fewer wildcards beats more, longer
    literal pattern beats shorter.  Higher is more specific."""
    tool_exact = 0 if any(c in rule.tool for c in "*?[") else 1
    arg_wildcards = sum(rule.args.count(c) for c in "*?[")
    return (tool_exact, -arg_wildcards, len(rule.args))


def _class_default(decision_class: DecisionClass, config: PolicyConfig) -> SideEffectClass:
    if decision_class is DecisionClass.A:
        return "auto"
    if decision_class is DecisionClass.B:
        return "ask"
    return config.class_c_default


def resolve(
    config: PolicyConfig,
    tool: Tool,
    arguments: dict[str, Any],
    decision_class: DecisionClass,
    workspace: Path | None = None,
) -> SideEffectClass:
    """Resolve the policy effect for a classified tool call.

    Most-specific matching rule wins; exact ties break toward the most
    restrictive effect.  No match → the decision-class default.  A class-C
    call never resolves to ``auto`` — policy cannot grant what the boundary
    forbids.
    """
    summary = summarize_arguments(tool, arguments, workspace)
    matches = [
        rule
        for rule in config.rules
        if fnmatch.fnmatchcase(tool.name, rule.tool) and fnmatch.fnmatchcase(summary, rule.args)
    ]
    if matches:
        best = max(_specificity(rule) for rule in matches)
        tied = [rule for rule in matches if _specificity(rule) == best]
        effect: SideEffectClass = "auto"
        for rule in tied:
            if _RESTRICTIVENESS[rule.effect] > _RESTRICTIVENESS[effect]:
                effect = rule.effect
    else:
        effect = _class_default(decision_class, config)

    if decision_class is DecisionClass.C and effect == "auto":
        return config.class_c_default
    return effect


# ── Persistence (.tst/config.yaml) ──────────────────────────────────────


def _config_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".tst" / "config.yaml"


def load_policy(workspace: str | Path) -> PolicyConfig:
    """Load the ``policy:`` section of ``.tst/config.yaml``.

    Returns the default (empty rules, C → ask) when the file or section is
    absent.  Other sections (``boundary:``, ``caps:``) are not this
    loader's concern.

    Raises:
        ConfigError: If the file is invalid YAML, not a mapping, or the
            policy section fails validation.  The message names the
            offending key.
    """
    path = _config_path(workspace)
    if not path.exists():
        return PolicyConfig()

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

    section: Any = data.get("policy", {})
    try:
        return PolicyConfig.model_validate(section)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise ConfigError(f"Invalid policy config in {path}: policy.{loc}: {first['msg']}") from e


def save_policy(workspace: str | Path, config: PolicyConfig) -> None:
    """Write the ``policy:`` section of ``.tst/config.yaml``.

    Section-preserving: ``boundary:``, ``caps:``, and any other keys are
    carried through untouched.  Atomic write (temp file + replace), the
    pattern TD-604 established for workspace files.
    """
    path = _config_path(workspace)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {path}: {e}") from e
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level")
        existing = loaded

    existing["policy"] = config.model_dump(mode="json")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
