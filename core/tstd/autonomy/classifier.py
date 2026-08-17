"""Decision classifier — static rule table (TD-701).

The classifier models the A/B/C decision classes from spec §12.2 and
classifies unambiguous proposed actions *without* a model call, using a
data-driven rule table.

Autonomy is a contract, not a switch (§12.1): inside a declared boundary
the agent has blanket authority, and it stops when it would step outside
the wall.  That wall is what the rule table encodes.  The rules here are
the *obvious* cases — the ones that must never reach the model:

- any path outside the workspace → C, always
- a network call to a host outside the allowlist → C
- a write to a steering file (``AGENTS.md`` / ``CLAUDE.md`` /
  ``.tst/rules/**``) → C, even inside the workspace
- a spent/spend/time/iteration cap that is already exceeded → C
- an in-workspace edit inside ``writable_paths`` → A

Anything the table cannot decide is left unclassified so the worker-tier
classifier (TD-703) can handle it, defaulting to **B** — fail toward
asking, never toward acting.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# ── Decision classes (spec §12.2) ─────────────────────────────────────


class DecisionClass(StrEnum):
    """The three decision classes an action is placed in before it runs.

    - **A — Taste:** reversible, inside the workspace, no contract with
      anything external.  Decide, log, never ask.
    - **B — Structural:** reversible but costly to unwind.  Decide if the
      charter covers it, log with rationale, surface in the summary.
    - **C — Boundary:** irreversible, outside the workspace, or over a
      declared cap.  Stop, always, notify.
    """

    A = "A"
    B = "B"
    C = "C"


# ── Boundary and request models ────────────────────────────────────────

_STEERING_BASENAMES = frozenset({"AGENTS.md", "CLAUDE.md"})
_STEERING_RULES_DIR_PARTS = (".tst", "rules")


@dataclass(frozen=True)
class Boundary:
    """The declared wall an action may not cross without asking.

    Attributes:
        workspace_root: Canonical path of the workspace.  ``None`` when the
            classifier is used before a workspace is established; in that
            case any path is treated as outside the workspace.
        writable_patterns: Glob patterns (relative to the workspace) the
            agent may write to.  Slash-less patterns match the basename at
            any depth; ``**`` matches everything.  Default is ``("**",)``,
            i.e. workspace-only writes.
        allowed_hosts: Hosts the agent may reach on the network.  Empty
            (the default) means no network calls are allowed.
        cap_exceeded: ``True`` when any declared cap (spend, wall-clock,
            iterations) is already over its limit.
    """

    workspace_root: Path | None
    writable_patterns: tuple[str, ...] = ("**",)
    allowed_hosts: frozenset[str] = frozenset()
    cap_exceeded: bool = False


@dataclass(frozen=True)
class DecisionRequest:
    """A proposed tool call, reduced to the signals the rule table needs.

    Path extraction from raw tool arguments is the caller's job (TD-702);
    by the time a request reaches the classifier the relevant paths, hosts
    and mutation intent are already resolved and absolute.

    Attributes:
        tool_name: The tool being called.
        arguments: The raw tool arguments, retained for diagnostics.
        reads: Absolute paths the call reads.
        writes: Absolute paths the call writes.
        hosts: Hosts the call would reach over the network.
        is_mutation: ``True`` when the call mutates state (writes) rather
            than only reading.
    """

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reads: tuple[Path, ...] = ()
    writes: tuple[Path, ...] = ()
    hosts: frozenset[str] = frozenset()
    is_mutation: bool = False


# ── Rule table ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    """A single classification rule.

    ``match`` is a pure predicate over the request and boundary.  Rules are
    evaluated in table order; the first match wins, so irreversible Class C
    rules are declared before the reversible Class A rule.
    """

    id: str
    description: str
    decision_class: DecisionClass
    match: Callable[[DecisionRequest, Boundary], bool]


@dataclass(frozen=True)
class Classification:
    """The outcome of running a request through the rule table.

    Attributes:
        decision_class: The class assigned, or ``None`` when no rule fired
            (the case is ambiguous and must go to the worker-tier
            classifier, TD-703).
        rule: The rule that fired, or ``None`` when unclassified.  Kept for
            explainability — every classification can say *which rule*
            decided it and why.
        reason: A short human-readable explanation of the decision.
    """

    decision_class: DecisionClass | None = None
    rule: Rule | None = None
    reason: str = ""


# ── Path resolution helpers ────────────────────────────────────────────


def canonical_path(path: Path) -> Path:
    """Resolve *path* to an absolute, symlink-free canonical form.

    Symlink traversal is resolved here so a symlink inside the workspace
    that points outside is correctly detected as outside (mirrors the
    boundary enforcement in TD-602).
    """
    return Path(os.path.abspath(os.path.realpath(path)))


def is_in_workspace(boundary: Boundary, path: Path) -> bool:
    """Whether *path* resolves to the workspace root or below it."""
    if boundary.workspace_root is None:
        return False
    return canonical_path(path).is_relative_to(canonical_path(boundary.workspace_root))


def relative_parts(path: Path, workspace_root: Path) -> list[str]:
    """Path segments of *path* relative to *workspace_root*, or ``[]`` if outside."""
    try:
        return list(canonical_path(path).relative_to(canonical_path(workspace_root)).parts)
    except ValueError:
        return []


def path_matches(pattern: str, rel_parts: list[str]) -> bool:
    """Match a workspace-relative path against a writable glob *pattern*.

    ``**`` spans zero or more segments; a pattern with no ``/`` matches the
    basename at any depth (same semantics as TD-503 ``appliesTo``).
    """
    pat = pattern.replace("\\", "/").strip("/")
    pat_parts = [p for p in pat.split("/") if p]
    if "/" not in pat:
        if pat == "**":
            return True
        # Slash-less pattern matches the basename at any depth.
        return bool(rel_parts) and any(fnmatch.fnmatchcase(seg, pat) for seg in rel_parts)
    return match_segments(pat_parts, list(rel_parts))


def match_segments(pat_parts: list[str], path_parts: list[str]) -> bool:
    """Segment-wise recursive matcher with ``**`` support."""
    if not pat_parts:
        return not path_parts
    head = pat_parts[0]
    if head == "**":
        for i in range(len(path_parts) + 1):
            if match_segments(pat_parts[1:], path_parts[i:]):
                return True
        return False
    if not path_parts:
        return False
    if fnmatch.fnmatchcase(path_parts[0], head):
        return match_segments(pat_parts[1:], path_parts[1:])
    return False


def is_steering_write(boundary: Boundary, path: Path) -> bool:
    """Whether *path* is a write target the daemon refuses (prime §2.4).

    Steering files — ``AGENTS.md``, ``CLAUDE.md``, and anything under
    ``.tst/rules/`` — are read-only to the filesystem tool, unconditionally.
    """
    relative = relative_parts(path, boundary.workspace_root) if boundary.workspace_root else []
    if not relative:
        return False
    basename = relative[-1].upper()
    if basename in {s.upper() for s in _STEERING_BASENAMES}:
        return True
    return tuple(relative[:2]) == _STEERING_RULES_DIR_PARTS


def writes_match_writable(boundary: Boundary, request: DecisionRequest) -> bool:
    """Whether every write target falls within ``writable_patterns``."""
    if boundary.workspace_root is None:
        return False
    for write in request.writes:
        rel_parts = relative_parts(write, boundary.workspace_root)
        if not rel_parts:
            # Write resolves outside the workspace — handled as C by another rule.
            return False
        if not any(path_matches(p, rel_parts) for p in boundary.writable_patterns):
            return False
    return True


# ── The rules (declared, in priority order) ────────────────────────────


def _rule_unsafe_path(req: DecisionRequest, boundary: Boundary) -> bool:
    """A path the call touches has a Windows-unsafe form (TD-602).

    Drive-relative/absolute, UNC, 8.3 short names, and alternate data
    streams are refused fail-closed by the guard; the classifier mirrors
    that so the audit event carries C for these refusals too.

    No canonical form is passed: the rule runs on every classification and
    the cheap string checks short-circuit ahead of the 8.3 rule, so a UNC
    path is judged without asking the OS to resolve ``\\\\server\\share``
    first.  Windows still expands short names there — ``windows_unsafe_reason``
    resolves for itself when it reaches that rule (TD-1406).
    """
    from ..tools.boundary import windows_unsafe_reason  # local import: no cycle

    return any(windows_unsafe_reason(str(p)) is not None for p in (*req.reads, *req.writes))


def _rule_outside_workspace(req: DecisionRequest, boundary: Boundary) -> bool:
    """Any path the call touches resolves outside the workspace root."""
    return any(not is_in_workspace(boundary, p) for p in (*req.reads, *req.writes))


def _rule_network_new_host(req: DecisionRequest, boundary: Boundary) -> bool:
    """The call reaches a host not on the network allowlist."""
    return any(host not in boundary.allowed_hosts for host in req.hosts)


def _rule_steering_write(req: DecisionRequest, boundary: Boundary) -> bool:
    """The call writes a steering file, regardless of writable_paths."""
    return req.is_mutation and any(is_steering_write(boundary, p) for p in req.writes)


def _rule_cap_exceeded(req: DecisionRequest, boundary: Boundary) -> bool:
    """A declared cap is already exceeded."""
    return boundary.cap_exceeded


def _rule_in_workspace_edit(req: DecisionRequest, boundary: Boundary) -> bool:
    """An in-workspace write inside writable_paths is a reversible Class A edit."""
    if not req.is_mutation or not req.writes:
        return False
    # The C-worthy writes (outside workspace / steering / cap) already
    # fired above; only reach this rule when none of them applied.
    all_in_workspace = all(is_in_workspace(boundary, p) for p in req.writes)
    return all_in_workspace and writes_match_writable(boundary, req)


def _rule_outside_writable(req: DecisionRequest, boundary: Boundary) -> bool:
    """An in-workspace write outside the declared ``writable_paths``.

    The charter's boundary forbids it (spec §12.2 "anything the charter
    forbids" → C), and the boundary guard refuses it at the tool layer
    (TD-602).  Making it a C rule keeps classification, enforcement, and
    the audit record consistent: the ``tool_call`` event carries C.
    """
    if not req.is_mutation or not req.writes:
        return False
    # Steering / outside-workspace writes fired earlier; only in-workspace
    # writes reach here.
    all_in_workspace = all(is_in_workspace(boundary, p) for p in req.writes)
    return all_in_workspace and not writes_match_writable(boundary, req)


RULE_TABLE: tuple[Rule, ...] = (
    Rule(
        id="boundary-unsafe-path",
        description="action touches a path with a Windows-unsafe form",
        decision_class=DecisionClass.C,
        match=_rule_unsafe_path,
    ),
    Rule(
        id="path-outside-workspace",
        description="action touches a path outside the workspace",
        decision_class=DecisionClass.C,
        match=_rule_outside_workspace,
    ),
    Rule(
        id="network-new-host",
        description="action reaches a network host outside the allowlist",
        decision_class=DecisionClass.C,
        match=_rule_network_new_host,
    ),
    Rule(
        id="steering-file-write",
        description="action writes a steering file (AGENTS.md/CLAUDE.md/.tst/rules)",
        decision_class=DecisionClass.C,
        match=_rule_steering_write,
    ),
    Rule(
        id="cap-exceeded",
        description="a declared spend/wall-clock/iteration cap is exceeded",
        decision_class=DecisionClass.C,
        match=_rule_cap_exceeded,
    ),
    Rule(
        id="path-outside-writable",
        description="action writes in-workspace but outside writable_paths",
        decision_class=DecisionClass.C,
        match=_rule_outside_writable,
    ),
    Rule(
        id="in-workspace-edit",
        description="in-workspace source edit within writable_paths",
        decision_class=DecisionClass.A,
        match=_rule_in_workspace_edit,
    ),
)


# ── Classifier ─────────────────────────────────────────────────────────


class DecisionClassifier:
    """Evaluates the static rule table over a proposed tool call.

    Usage::

        boundary = Boundary(workspace_root=Path("/work/proj"))
        classifier = DecisionClassifier(boundary)
        decision = classifier.classify(
            DecisionRequest(tool_name="fs_edit", writes=(Path("/work/proj/src/a.py"),),
                            is_mutation=True)
        )
        assert decision.decision_class is DecisionClass.A
        assert decision.rule.id == "in-workspace-edit"

    Unambiguous cases return a classification naming the rule that fired;
    ambiguous ones return a classification with ``decision_class is None``
    so the worker-tier classifier (TD-703) can decide, defaulting to B.
    """

    def __init__(self, boundary: Boundary) -> None:
        self.boundary = boundary

    def classify(self, request: DecisionRequest) -> Classification:
        """Classify *request* against the rule table.

        Returns:
            A :class:`Classification` naming the first firing rule, or one
            with ``decision_class``/``rule`` as ``None`` if no rule fired.
        """
        for rule in RULE_TABLE:
            if rule.match(request, self.boundary):
                return Classification(
                    decision_class=rule.decision_class,
                    rule=rule,
                    reason=f"[{rule.id}] {rule.description}",
                )
        return Classification(decision_class=None, rule=None, reason="no rule fired")
