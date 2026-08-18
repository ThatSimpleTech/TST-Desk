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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from .autonomy.classifier import DecisionClass
from .config import ConfigError

if TYPE_CHECKING:
    # Type hints only.  A runtime import would cycle: tools/__init__ →
    # dispatch → policy (TD-802 wires the policy gate into dispatch).
    from .tools.registry import Tool

# ── Models ──────────────────────────────────────────────────────────────

# Same vocabulary as tools.registry.SideEffectClass, defined here so policy
# stays importable from dispatch without a cycle (TD-802).
PolicyEffect = Literal["auto", "ask", "never"]

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
    effect: PolicyEffect


class PolicyConfig(BaseModel):
    """The ``policy:`` section of ``.tst/config.yaml``."""

    rules: list[PolicyRule] = Field(default_factory=list)
    class_c_default: Literal["ask", "never"] = Field(
        default="ask",
        description="Effect for class-C calls with no matching rule: ask (default) or never",
    )
    approval_timeout_seconds: float | None = Field(
        default=None,
        ge=0,
        description="How long an approval request waits before being treated "
        "as a denial.  None (the default) waits indefinitely (TD-802).",
    )


@dataclass(frozen=True)
class PolicyDecision:
    """The outcome of resolving a call against policy, with provenance.

    ``rule`` is the rule that decided the effect, or ``None`` when the
    decision-class default applied.  ``reason`` is the human-readable
    explanation carried by ``approval_request`` (TD-802).
    """

    effect: PolicyEffect
    reason: str
    rule: PolicyRule | None


@dataclass(frozen=True)
class ApprovalOutcome:
    """The result of an approval round-trip (TD-802).

    ``message`` is the structured denial text returned to the model on a
    non-approval (user denial or timeout); empty when approved.
    """

    approved: bool
    message: str = ""


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


def _class_default(decision_class: DecisionClass, config: PolicyConfig) -> PolicyEffect:
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
    skip_all: bool = False,
) -> PolicyEffect:
    """Resolve the policy effect for a classified tool call.

    Most-specific matching rule wins; exact ties break toward the most
    restrictive effect.  No match → the decision-class default.  A class-C
    call never resolves to ``auto`` — policy cannot grant what the boundary
    forbids.  Thin wrapper over :func:`resolve_explained` (TD-802).
    """
    return resolve_explained(
        config, tool, arguments, decision_class, workspace, skip_all=skip_all
    ).effect


def resolve_explained(
    config: PolicyConfig,
    tool: Tool,
    arguments: dict[str, Any],
    decision_class: DecisionClass,
    workspace: Path | None = None,
    skip_all: bool = False,
) -> PolicyDecision:
    """Like :func:`resolve`, but carries the deciding rule and a
    human-readable reason for the approval gate (TD-802).

    ``skip_all`` (TD-804) promotes a Class B ``ask`` to ``auto``. It
    cannot make Class C automatic and cannot override a ``never`` rule.
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
        # Most restrictive effect wins among exact ties; the first tied rule
        # carrying it stands as provenance.
        winner = max(tied, key=lambda r: _RESTRICTIVENESS[r.effect])
        decision = PolicyDecision(
            winner.effect,
            f"policy rule `{winner.tool}: {winner.args}` → {winner.effect}",
            winner,
        )
    else:
        effect = _class_default(decision_class, config)
        reason = {
            DecisionClass.A: "decision class A runs automatically",
            DecisionClass.B: "decision class B requires approval",
            DecisionClass.C: (
                "decision class C requires approval"
                if config.class_c_default == "ask"
                else "decision class C is forbidden by policy"
            ),
        }[decision_class]
        decision = PolicyDecision(effect, reason, None)

    if decision_class is DecisionClass.C and decision.effect == "auto":
        # The wall: no rule may downgrade a class-C call to silent execution.
        return PolicyDecision(
            config.class_c_default,
            f"{decision.reason}, but decision class C may not run automatically",
            decision.rule,
        )
    # TD-804: skip-all takes the *ask*, not the wall. A never rule and
    # Class C stay exactly as they resolved.
    if skip_all and decision.effect == "ask" and decision_class is not DecisionClass.C:
        return PolicyDecision(
            "auto",
            "skip-all approvals is on",
            decision.rule,
        )
    return decision


def format_summary(tool: Tool, arguments: dict[str, Any], workspace: Path | None = None) -> str:
    """Human-readable one-liner for an approval card (TD-802).

    "Write src/app.py" / "Read src/app.py" / "Run `npm test`" /
    "Reach api.example.com" / "Call tool({json})".
    """
    summary = summarize_arguments(tool, arguments, workspace)
    if tool.path_fields:
        verb = "Write" if tool.mutates else "Read"
        return f"{verb} {summary}"
    if isinstance(arguments.get("command"), str):
        return f"Run `{summary}`"
    if tool.host_fields:
        return f"Reach {summary}"
    return f"Call {tool.name}({summary})"


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

    # An empty or comment-only file (e.g. the scaffolded template, TD-1103)
    # means "no rules", not an error.
    if data is None:
        return PolicyConfig()

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
        if loaded is None:
            loaded = {}  # empty or comment-only file — start sections fresh
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


# ── External-import approvals (TD-505) ─────────────────────────────────


def load_approved_imports(workspace: str | Path) -> frozenset[Path]:
    """Load the ``approved_external_imports`` allowlist from ``.tst/config.yaml``.

    Returns the absolute paths of external imports the user has approved
    for *workspace*.  Empty when the file or key is absent.  Entries are
    strings; each is expanded (``~``) and resolved so it compares cleanly
    against the assembler's resolved import paths.

    Raises:
        ConfigError: If the file is invalid YAML, not a mapping, or the
            key is not a list of non-empty strings.
    """
    path = _config_path(workspace)
    if not path.exists():
        return frozenset()
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    # An empty or comment-only file (the TD-1103 scaffold) means no approvals.
    if data is None:
        return frozenset()
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    raw: Any = data.get("approved_external_imports", [])
    if not isinstance(raw, list):
        raise ConfigError(f"Invalid approved_external_imports in {path}: expected a list")

    result: set[Path] = set()
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(f"Invalid approved_external_imports entry in {path}: {entry!r}")
        result.add(Path(entry).expanduser().resolve())
    return frozenset(result)


def save_approved_imports(workspace: str | Path, paths: Iterable[Path]) -> None:
    """Write the ``approved_external_imports`` allowlist (TD-505).

    Section-preserving like :func:`save_policy`: only this one key is
    touched; ``policy``, ``boundary``, and ``caps`` pass through.  Atomic
    write (temp file + replace).
    """
    path = _config_path(workspace)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {path}: {e}") from e
        if loaded is None:
            loaded = {}  # empty or comment-only file — start sections fresh
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level")
        existing = loaded

    existing["approved_external_imports"] = sorted(str(p) for p in paths)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# ── Always-allow (TD-803) ──────────────────────────────────────────────


def propose_always_allow(
    tool: Tool,
    arguments: dict[str, Any],
    decision_class: DecisionClass,
    workspace: Path | None = None,
) -> PolicyRule | None:
    """Generate the narrowest "always allow" rule for a classified call.

    The rule is scoped to the exact argument summary — never a blanket
    ``args="**"`` grant for the whole tool.  A class-C call returns ``None``:
    the boundary can never be always-allowed, regardless of how the rule is
    phrased (criterion 4).
    """
    if decision_class is DecisionClass.C:
        return None
    return PolicyRule(
        tool=tool.name,
        args=summarize_arguments(tool, arguments, workspace),
        effect="auto",
    )


def add_rule(config: PolicyConfig, rule: PolicyRule) -> PolicyConfig:
    """Add a rule, replacing any existing rule with the same ``(tool, args)``.

    Idempotent so re-affirming "always allow" for the same call doesn't pile
    up duplicate rules (they would shadow each other and make the settings
    list noisy).
    """
    config.rules = [r for r in config.rules if (r.tool, r.args) != (rule.tool, rule.args)]
    config.rules.append(rule)
    return config


def remove_rule(config: PolicyConfig, tool: str, args: str) -> bool:
    """Remove every rule matching ``(tool, args)``; return whether any changed.

    ``(tool, args)`` is the rule identity used for individual revocation in
    settings (TD-803).
    """
    before = len(config.rules)
    config.rules = [r for r in config.rules if (r.tool, r.args) != (tool, args)]
    return len(config.rules) < before


# ── Skip-all (TD-804) ──────────────────────────────────────────────────
#
# Machine-wide, not workspace-wide: a `.tst/config.yaml` bit would be
# committed and surprise the next clone.  Lives next to the daemon's
# other user-data files.


def skip_all_path(data_dir: str | Path) -> Path:
    """Path of the user-data file that holds the skip-all bit."""
    return Path(data_dir) / "approvals.yaml"


def load_skip_all(data_dir: str | Path) -> bool:
    """Load skip-all from the user data dir. Absent or unreadable is off."""
    path = skip_all_path(data_dir)
    if not path.exists():
        return False
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    # Only YAML true counts. bool("false") is True, which is the wrong
    # read of a hand-edited file.
    return data.get("skip_all") is True


def save_skip_all(data_dir: str | Path, enabled: bool) -> None:
    """Persist skip-all atomically in the user data dir."""
    path = skip_all_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".approvals.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump({"skip_all": enabled}, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
