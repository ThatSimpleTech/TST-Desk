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
  ``.tst/rules/**``, slash-command trees ``.tst/commands/**`` /
  ``.claude/commands/**`` — TD-4501, any ``SKILL.md`` — TD-4502, and the
  signed charter at ``.tst/autonomy/CHARTER.md`` — TD-4001) → C, even
  inside the workspace
- a write to a path that runs code later (any ``.git``, ``.vscode``,
  ``.idea`` or ``.husky`` segment; git's global folder ``.config/git``;
  ``.envrc``, ``.gitconfig``, shell startup files and git hook-manager
  configs by basename) → C, through the fs tools and the shell
  write-target table, read as ``cmd.exe`` reads it too on Windows
  (TD-5108, TD-5157)
- a write under ``.tst/memory/`` → A (spec §5; not steering)
- a spent/spend/time/iteration cap that is already exceeded → C
- an in-workspace edit inside ``writable_paths`` → A
- a built-in read (``fs_read`` / ``fs_list`` / ``fs_grep``) inside the
  workspace → A, with no worker call (TD-5141); a user policy rule
  (``ask`` / ``never``) still decides whether it runs
- a shell command made only of the static read-only list → A (TD-5103);
  every other shell command → B (TD-4805)
- an MCP tool with no declared path or host metadata → B, never A
  (TD-4402; even if ``side_effect_class`` was reset to ``auto``)

Anything the table cannot decide is left unclassified so the worker-tier
classifier (TD-703) can handle it, defaulting to **B** — fail toward
asking, never toward acting.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..connectors.github_classify import GITHUB_READ_TOOLS
from ..policy_shell import host_dialects
from ..shell_read_only import match as read_only_match
from ..shell_read_only import probe_repo_config
from .charter import CHARTER_RELATIVE_PARTS
from .cmd_targets import cmd_write_targets

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

_STEERING_BASENAMES = frozenset({"AGENTS.md", "CLAUDE.md", "SKILL.md"})
_STEERING_RULES_DIR_PARTS = (".tst", "rules")
# Slash-command trees (TD-4501). Same Class C refusal as steering so the
# model sees one read-only rule. ``.tstdesk/commands`` covers a
# writable_paths hole that could reach the user-global tree.
_COMMAND_DIR_PAIRS = frozenset(
    {
        (".tst", "commands"),
        (".claude", "commands"),
        (".tstdesk", "commands"),
    }
)
_MEMORY_DIR_PARTS = (".tst", "memory")
# The shipped read tools (TD-5141). The rule also requires no provenance,
# so a plugin or MCP tool that borrows one of these names is not covered.
_BUILTIN_READ_TOOLS = frozenset({"fs_read", "fs_list", "fs_grep"})
# The approval policy lives here (spec §6). Not a steering file by name,
# but a write to it rewrites the guardrails — the self-escalation the
# steering refusal exists to prevent (TD-4803).
_POLICY_FILE_PARTS = (".tst", "config.yaml")
# The signed charter (spec §12.4, TD-4001): the human's contract with the
# run, committed before start. Read-only to the agent, Class C like
# steering — while its sibling DECISIONS.md ledger stays writable, so only
# the single file is protected, not the autonomy directory.  Folded from
# charter.CHARTER_RELATIVE_PARTS: one source of truth for the path.
_CHARTER_FILE_PARTS = tuple(p.casefold() for p in CHARTER_RELATIVE_PARTS)
# Paths that run code later (TD-5108). Writing one runs nothing now, but
# git, a login shell, direnv or an editor runs what it says the next time
# it starts, outside the classifier and the wall. Matched at any depth: a
# nested ``.git`` holding HEAD and config makes its folder a repo, direnv
# loads the ``.envrc`` of any folder you cd into, and an editor opens a
# nested project with its own ``.vscode``. The hook managers are here
# because the user's own ``git commit`` runs what they name; the daemon's
# own git runs no hooks (``git_safe``). Lefthook reads any of its base or
# ``-local`` names, dotted or not, in four formats.
# Stored case-folded; compared through ``_fold``.
_EXEC_LATER_DIRS = frozenset({".git", ".vscode", ".idea", ".husky"})
_LEFTHOOK_FILES = frozenset(
    f"{dot}{stem}.{ext}"
    for dot in ("", ".")
    for stem in ("lefthook", "lefthook-local")
    for ext in ("yml", "yaml", "toml", "json")
)
_EXEC_LATER_FILES = frozenset(
    {
        ".envrc",
        ".gitconfig",
        ".profile",
        ".bashrc",
        ".bash_profile",
        ".bash_login",
        ".bash_logout",
        ".bash_aliases",
        ".zshrc",
        ".zshenv",
        ".zprofile",
        ".zlogin",
        ".zlogout",
        ".pre-commit-config.yaml",
        *_LEFTHOOK_FILES,
    }
)


# git's global config folder (TD-5157): ``$XDG_CONFIG_HOME/git``, else
# ``~/.config/git``. Its ``config`` is read like ``~/.gitconfig`` and can
# include other files, ``attributes`` routes files through filter drivers,
# and ``ignore`` sits beside them. A segment naming the folder that holds
# it is ``.config`` or a reference to ``XDG_CONFIG_HOME`` as a shell or
# cmd command writes it (``$XDG_CONFIG_HOME``, ``${XDG_CONFIG_HOME:-…}``,
# ``%XDG_CONFIG_HOME%``); the variable's own value is checked separately.
_GIT_GLOBAL_DIR = "git"


def _names_config_home(segment: str) -> bool:
    return segment.strip("{}\"'") == ".config" or "xdg_config_home" in segment


def _git_global_dirs() -> list[Path]:
    """Where git looks for its global folder: ``~/.config/git`` always counts.

    Git reads ``$XDG_CONFIG_HOME/git`` when the variable is set and
    ``~/.config/git`` when it is not. The default is checked either way:
    the shell or the user's own git may not see the daemon's environment.
    """
    dirs: list[Path] = []
    with contextlib.suppress(RuntimeError, KeyError):  # no home folder to find
        dirs.append(Path.home() / ".config" / _GIT_GLOBAL_DIR)
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg and os.path.isabs(xdg):
        dirs.append(Path(xdg) / _GIT_GLOBAL_DIR)
    return dirs


def _in_git_global_dir(folded: Sequence[str], path: Path) -> bool:
    """Whether *path* is in, or is, git's global config folder.

    By name (a ``.config/git`` pair of segments, or ``XDG_CONFIG_HOME``
    written out), else by where it is: a workspace that is ``~/.config``
    itself only shows ``git/config`` by name.
    """
    if any(_names_config_home(a) and b == _GIT_GLOBAL_DIR for a, b in pairwise(folded)):
        return True
    where = _fold(canonical_path(path).parts)
    for folder in _git_global_dirs():
        base = _fold(canonical_path(folder).parts)
        if where[: len(base)] == base:
            return True
    return False


def _fold(parts: Sequence[str]) -> tuple[str, ...]:
    """Case-fold path parts for guard comparisons (TD-4804).

    APFS and NTFS — the shipped-default filesystems — are case-insensitive,
    so a verbatim tuple match lets ``.tst/RULES/…`` past the steering
    check while the filesystem lands it in ``.tst/rules/``. Folding is
    unconditional: on a case-sensitive filesystem a literal ``.tst/RULES/``
    directory is over-refused as steering, and that is accepted — the
    guard fails closed, and a case-variant of a reserved name is never
    legitimate.
    """
    return tuple(p.casefold() for p in parts)


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
        shell_read_only: The read-only shell list entries in force
            (TD-5103, ``shell.read_only`` in ``config.yaml``). Empty, the
            default, means no shell command is Class A.
        read_guards: The ``args`` globs of the user's ``ask`` and
            ``never`` rules for the built-in reads, read on every call so
            a rule saved mid-session counts (TD-5103,
            :func:`tstd.policy.read_guards`). A listed shell command that
            names a file one matches, or that can print a file it does not
            name, is not Class A while there is one.
    """

    workspace_root: Path | None
    writable_patterns: tuple[str, ...] = ("**",)
    allowed_hosts: frozenset[str] = frozenset()
    cap_exceeded: bool = False
    shell_read_only: frozenset[str] = frozenset()
    read_guards: Callable[[], Sequence[str]] = field(default=lambda: (), compare=False)


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
        actuates: Desktop computer-use only (TD-3301). ``None`` means the
            existing path/host table applies. ``False`` is capture-only
            (Class A). ``True`` is actuation (Class B).
        side_effect_class: The tool's declared approval floor (TD-4808).
            ``never`` classifies C alongside the other refusals, ahead of
            every grant; ``ask`` is at least B via the terminal floor
            rule, so specific grants (memory, in-workspace edit) keep
            their deliberate class.
        provenance: Copied from ``Tool.provenance``. MCP tools are
            ``mcp:<server-id>`` (TD-4402). Builtins leave this ``None``.
        has_path_host_metadata: ``True`` when the tool declared
            ``path_fields``, ``host_fields``, or a ``host_resolver``.
            An MCP tool without that declaration cannot be Class A.
    """

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reads: tuple[Path, ...] = ()
    writes: tuple[Path, ...] = ()
    hosts: frozenset[str] = frozenset()
    is_mutation: bool = False
    actuates: bool | None = None
    side_effect_class: str = "auto"
    provenance: str | None = None
    has_path_host_metadata: bool = False


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
    #: What let the call through, for the audit (TD-5103): the read-only
    #: list entries a ``shell-read-only`` call used.
    allowed_by: Callable[[DecisionRequest, Boundary], tuple[str, ...]] | None = None


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
        allowed_by: The read-only list entries that made a shell call
            Class A (TD-5103); empty for every other call.
    """

    decision_class: DecisionClass | None = None
    rule: Rule | None = None
    reason: str = ""
    allowed_by: tuple[str, ...] = ()


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


def is_memory_write(boundary: Boundary, path: Path) -> bool:
    """Whether *path* is under ``.tst/memory/`` (spec §5).

    Memory is git-tracked and reversible. It is not steering: the two
    trees share ``.tst/`` and must not share a glob. ``AGENTS.md`` /
    ``CLAUDE.md`` as a basename stay steering even if dropped here.
    """
    relative = relative_parts(path, boundary.workspace_root) if boundary.workspace_root else []
    if len(relative) < 2 or _fold(relative[:2]) != _MEMORY_DIR_PARTS:
        return False
    return relative[-1].upper() not in {s.upper() for s in _STEERING_BASENAMES}


def _has_command_dir(parts: Sequence[str]) -> bool:
    """Whether *parts* contain a reserved slash-command directory pair."""
    folded = _fold(parts)
    return any((folded[i], folded[i + 1]) in _COMMAND_DIR_PAIRS for i in range(len(folded) - 1))


def is_steering_write(boundary: Boundary, path: Path) -> bool:
    """Whether *path* is a write target the daemon refuses (prime §2.4).

    Steering files — ``AGENTS.md``, ``CLAUDE.md``, any ``SKILL.md``
    (TD-4502), anything under
    ``.tst/rules/``, slash-command trees (``.tst/commands/``,
    ``.claude/commands/``, and ``.tstdesk/commands/`` — TD-4501), the
    approval policy at ``.tst/config.yaml`` (TD-4803), and the signed
    charter at ``.tst/autonomy/CHARTER.md`` (TD-4001; spec §12.4) — are
    read-only to the agent, unconditionally.  The charter's own
    directory stays otherwise writable: ``DECISIONS.md`` there is the
    ledger the loop appends to.  ``.tst/memory/`` is the carve-out
    (TD-2102); never fold it into ``.tst/**``.  Directory comparisons
    case-fold (TD-4804): see ``_fold``.
    """
    # Full-path pair check first so ~/.tstdesk/commands is refused even
    # when the workspace wall would otherwise treat it as outside (a
    # writable_paths hole) or when relative_parts is empty.
    if _has_command_dir(path.parts):
        return True
    relative = relative_parts(path, boundary.workspace_root) if boundary.workspace_root else []
    if not relative:
        return False
    basename = relative[-1].upper()
    if basename in {s.upper() for s in _STEERING_BASENAMES}:
        return True
    if is_memory_write(boundary, path):
        return False
    if _fold(relative) == _POLICY_FILE_PARTS:
        return True
    if _fold(relative) == _CHARTER_FILE_PARTS:
        return True
    if _has_command_dir(relative):
        return True
    return _fold(relative[:2]) == _STEERING_RULES_DIR_PARTS


def is_exec_later_write(boundary: Boundary, path: Path) -> bool:
    """Whether writing *path* can make code run later (TD-5108).

    True for any path with a ``.git``, ``.vscode``, ``.idea`` or
    ``.husky`` segment, anything in git's global folder (``.config/git``
    or ``$XDG_CONFIG_HOME/git``, TD-5157), and for shell startup files,
    ``.envrc``, ``.gitconfig`` and git hook-manager configs by basename,
    at any depth.  Inside the workspace only the workspace-relative
    segments count, so a workspace that itself lives under such a folder
    is not caught whole.  Outside it the whole canonical path counts: a
    shell redirect to ``~/.bashrc`` has no workspace-relative form.
    Segments case-fold (TD-4804).
    """
    root = boundary.workspace_root
    if root is not None and is_in_workspace(boundary, path):
        parts = relative_parts(path, root)
    else:
        parts = list(canonical_path(path).parts)
    # The path as written counts too (TD-5157): a shell redirect to
    # ~/.config/git/config that is a symlink into a dotfiles workspace
    # resolves to a name that says nothing.
    lexical = Path(os.path.abspath(path))
    written = list(lexical.parts)
    roots = () if root is None else (Path(os.path.abspath(root)), canonical_path(root))
    for base in roots:
        if lexical.is_relative_to(base):
            written = list(lexical.relative_to(base).parts)
            break
    return any(_names_exec_later(_fold(view), path) for view in (parts, written))


def _names_exec_later(folded: tuple[str, ...], path: Path) -> bool:
    if not folded:
        return False
    if folded[-1] in _EXEC_LATER_FILES or any(p in _EXEC_LATER_DIRS for p in folded):
        return True
    return _in_git_global_dir(folded, path)


def writes_match_writable(boundary: Boundary, request: DecisionRequest) -> bool:
    """Whether every write target falls within ``writable_patterns``."""
    if boundary.workspace_root is None:
        return False
    for write in request.writes:
        rel_parts = relative_parts(write, boundary.workspace_root)
        if not rel_parts:
            # Write resolves outside the workspace — handled as C by another rule.
            return False
        # Spec §5: the agent may write memory even when writable_paths is tight.
        if is_memory_write(boundary, write):
            continue
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


def _rule_exec_later_write(req: DecisionRequest, boundary: Boundary) -> bool:
    """The call writes a path that runs code later (TD-5108).

    Class C, not a refusal: the user can approve it, unlike a steering
    write.  It sits ahead of ``memory-file-write`` so a ``.git`` planted
    under ``.tst/memory/`` is not waved through as memory.
    """
    return req.is_mutation and any(is_exec_later_write(boundary, p) for p in req.writes)


# ── Shell command targets (TD-4805) ────────────────────────────────────
#
# The fs tools declare their paths; the shell's targets live inside an
# opaque command string.  The static table extracts the common scripted
# write forms — redirection and ``tee`` — and judges them against the
# steering set and the paths that run code later (TD-5108).  Everything
# else about a shell command stays invisible to the table, which is why
# the shell also gets a Class B floor: a model's reading of an opaque
# string is never the sole gate on running it.

_REDIRECT_TOKENS = frozenset({">", ">>", "&>", "&>>", ">&", ">|"})
_SEGMENT_SEPARATORS = frozenset({"|", "&", ";", "||", "&&"})


def _shell_write_targets(command: str) -> list[str]:
    """Best-effort write-target extraction from a shell command string.

    Tokenizes with ``punctuation_chars`` so ``> file`` and ``>file`` both
    split.  Covers redirection (``>``, ``>>``, ``&>``, ``&>>``, plus
    ``>&``/``>|`` — TD-4817; descriptor dups like ``2>&1`` extract a
    numeric non-target, which never matches a steering path) and ``tee``
    arguments.  A zsh clobber mark rides the target as a leading ``!``
    (``>!file``, ``>&!file``, spaced ``>! file``) and is stripped before
    the steering check.  Quoted operators can still split on older
    Pythons, so a mention of a steering name inside quotes may
    over-classify as C — fail-safe, and rare next to the unquoted forms
    scripts actually use.

    Other write forms (``cp``, ``mv``, ``sed -i``, editors) are not
    parsed: the command falls through to the B floor and asks, which is
    where an opaque string belongs.  An unparseable command yields no
    targets here — it is not thereby judged safe, just not statically C.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=">|&;")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    targets: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _REDIRECT_TOKENS and i + 1 < len(tokens):
            target = tokens[i + 1]
            advance = 2
            if target == "!" and i + 2 < len(tokens):
                # zsh spaced clobber: `>! file` / `>&! file`
                target = tokens[i + 2]
                advance = 3
            target = target.lstrip("!")
            if target:
                targets.append(target)
            i += advance
            continue
        if token == "tee":
            j = i + 1
            while j < len(tokens) and tokens[j] not in _SEGMENT_SEPARATORS:
                if not tokens[j].startswith("-"):
                    targets.append(tokens[j])
                j += 1
            i = j
            continue
        i += 1
    return targets


def _shell_target_paths(req: DecisionRequest, boundary: Boundary) -> list[Path]:
    """The shell call's redirect and ``tee`` targets, workspace-joined."""
    if req.tool_name != "shell" or boundary.workspace_root is None:
        return []
    command = req.arguments.get("command")
    if not isinstance(command, str):
        return []
    root = boundary.workspace_root
    tokens = _shell_write_targets(command)
    if "cmd" in host_dialects():
        # Windows runs it through cmd /c, where \ separates folders (TD-5157).
        tokens += cmd_write_targets(command)
    targets: list[Path] = []
    for token in dict.fromkeys(tokens):
        target = Path(token)
        targets.append(target if target.is_absolute() else root / target)
    return targets


def _rule_shell_steering_write(req: DecisionRequest, boundary: Boundary) -> bool:
    """A shell command redirects or tees into a steering path (TD-4805)."""
    return any(is_steering_write(boundary, t) for t in _shell_target_paths(req, boundary))


def _rule_shell_exec_later_write(req: DecisionRequest, boundary: Boundary) -> bool:
    """A shell command redirects or tees into a path that runs code later (TD-5108)."""
    return any(is_exec_later_write(boundary, t) for t in _shell_target_paths(req, boundary))


def _read_only_entries(req: DecisionRequest, boundary: Boundary) -> tuple[str, ...]:
    """The read-only list entries covering a builtin shell call (TD-5103).

    Only the shipped tool qualifies: an MCP or plugin tool named ``shell``
    carries provenance and gets no grant.
    """
    command = req.arguments.get("command")
    if req.tool_name != "shell" or req.provenance is not None or not isinstance(command, str):
        return ()
    return read_only_match(
        command, boundary.shell_read_only, boundary.workspace_root, boundary.read_guards()
    )


async def prepare_read_only(req: DecisionRequest, boundary: Boundary) -> None:
    """Read what the read-only rule needs that only a subprocess can tell (TD-5160).

    The rule is static, so the git config a listed git command will see is
    read here, just before the call is classified. Without it, git entries
    ask. It is read with the list off too: the pins, and so the object
    format, apply to an approved git command as well.
    """
    command = req.arguments.get("command")
    if req.tool_name != "shell" or req.provenance is not None or not isinstance(command, str):
        return
    if boundary.workspace_root is not None:
        await probe_repo_config(command, boundary.workspace_root)


def _rule_shell_floor(req: DecisionRequest, boundary: Boundary) -> bool:
    """Every shell command is at least Class B (TD-4805), unless the
    static read-only list covers it (TD-5103).

    The static table cannot see a shell command's targets, so the only
    path to A was the worker tier's reading of an opaque string.  Auto-run
    for shell belongs to the user's saved always-allow rules (TD-803) and
    to the fixed, parsed list in :mod:`tstd.shell_read_only`, never to
    model judgment.
    """
    return req.tool_name == "shell" and not _read_only_entries(req, boundary)


def _rule_shell_read_only(req: DecisionRequest, boundary: Boundary) -> bool:
    """Every command of the line is on the read-only list (TD-5103).

    Static, so no worker call: the list is code, parsed, and only
    shrinks by config.  It sits after ``mcp-undeclared-fields`` like
    every A grant, and ``shell-floor`` above stands aside only for it.
    A user policy rule still decides first: policy falls back to the
    class default only when no rule matches.
    """
    return bool(_read_only_entries(req, boundary))


def _rule_mcp_undeclared_fields(req: DecisionRequest, _boundary: Boundary) -> bool:
    """MCP tools with no declared path/host metadata are never Class A.

    Missing ``path_fields`` and ``host_fields`` (and no ``host_resolver``)
    fail toward B, even if someone re-registers the tool as
    ``side_effect_class="auto"`` so the ask floor would not fire.  The
    worker must not grant A either — this is a static match, not a
    fall-through.  Declared host/path metadata leaves the rule silent so
    ``network-new-host`` and in-workspace writes still apply.  Computer-use
    tools are ``tstd.desktop``, not ``mcp:`` provenance.
    """
    provenance = req.provenance
    if provenance is None or not provenance.startswith("mcp:"):
        return False
    return not req.has_path_host_metadata


def _rule_memory_write(req: DecisionRequest, boundary: Boundary) -> bool:
    """The call writes only under ``.tst/memory/`` (Class A, TD-2102)."""
    return (
        req.is_mutation
        and bool(req.writes)
        and all(is_memory_write(boundary, p) for p in req.writes)
    )


def _rule_cap_exceeded(req: DecisionRequest, boundary: Boundary) -> bool:
    """A declared cap is already exceeded."""
    return boundary.cap_exceeded


def _rule_desktop_capture(req: DecisionRequest, _boundary: Boundary) -> bool:
    """A desktop capture tool cannot actuate (TD-3301)."""
    return req.actuates is False


def _rule_desktop_actuation(req: DecisionRequest, _boundary: Boundary) -> bool:
    """Desktop pointer/keyboard actuation is Class B (TD-3301)."""
    return req.actuates is True


def _rule_send_file(req: DecisionRequest, boundary: Boundary) -> bool:
    """``send_file`` copies workspace files into the session's own data dir (TD-5143).

    The copy is daemon state, not a workspace write, as a ``desktop-capture``
    screenshot is. Only the builtin matches: an MCP or plugin tool of that
    name carries provenance and gets no grant. An outside path never gets
    here: ``boundary-unsafe-path`` and ``path-outside-workspace`` come first.
    """
    return (
        req.tool_name == "send_file"
        and req.provenance is None
        and not req.is_mutation
        and bool(req.reads)
        and not req.writes
        and not req.hosts
        and all(is_in_workspace(boundary, p) for p in req.reads)
    )


def _rule_steering_proposal(req: DecisionRequest, boundary: Boundary) -> bool:
    """``propose_steering_edit`` writes nothing (TD-5140).

    It parks a diff for the user; only the user's Accept on the card
    writes the file, through the daemon, never through a tool. Asking
    here as well would put two approvals on one edit. Only the builtin
    matches, with exactly one in-workspace read path: an MCP or plugin
    tool of that name carries provenance and gets no grant, and an
    outside path was Class C above.
    """
    return (
        req.tool_name == "propose_steering_edit"
        and req.provenance is None
        and req.side_effect_class == "auto"
        and not req.is_mutation
        and len(req.reads) == 1
        and not req.writes
        and not req.hosts
        and is_in_workspace(boundary, req.reads[0])
    )


def _rule_consult(req: DecisionRequest, _boundary: Boundary) -> bool:
    """``consult`` asks the brain or validator one question (TD-5119).

    It writes nothing, runs nothing and reaches only the session's own
    model endpoints, and config caps it per turn on top of the spend
    cap. Asking on each one would defeat a worker run that consults
    mid-task. Only the builtin matches, with no path or host on the
    request: an MCP or plugin tool of that name carries provenance and
    gets no grant. A policy rule can still make it ask.
    """
    return (
        req.tool_name == "consult"
        and req.provenance is None
        and req.side_effect_class == "auto"
        and req.actuates is None
        and not req.is_mutation
        and not req.reads
        and not req.writes
        and not req.hosts
    )


def _rule_in_workspace_edit(req: DecisionRequest, boundary: Boundary) -> bool:
    """An in-workspace write inside writable_paths is a reversible Class A edit."""
    if not req.is_mutation or not req.writes:
        return False
    # The C-worthy writes (outside workspace / steering / cap) already
    # fired above; only reach this rule when none of them applied.
    all_in_workspace = all(is_in_workspace(boundary, p) for p in req.writes)
    return all_in_workspace and writes_match_writable(boundary, req)


def _rule_builtin_read(req: DecisionRequest, boundary: Boundary) -> bool:
    """A built-in read inside the workspace is Class A (TD-5141).

    Adam pre-approved the shipped read tools on 2026-10-14, ``.env``
    included: a read changes nothing and the wall already refuses every
    path outside the workspace (``path-outside-workspace`` fired above).
    Before this rule the worker tier judged each read, a model call per
    file for an answer that was always A.

    Narrow on purpose: a builtin (no provenance), still declared
    ``auto``, no writes, no hosts, and every read path canonically in the
    workspace (symlinks resolved). A user policy rule still wins, since
    policy only falls back to the class default when no rule matches.
    """
    return (
        req.tool_name in _BUILTIN_READ_TOOLS
        and req.provenance is None
        and req.side_effect_class == "auto"
        and not req.is_mutation
        and not req.writes
        and not req.hosts
        and bool(req.reads)
        and all(is_in_workspace(boundary, p) for p in req.reads)
    )


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
        description="action writes a steering, slash-command, or SKILL.md file",
        decision_class=DecisionClass.C,
        match=_rule_steering_write,
    ),
    Rule(
        id="shell-steering-write",
        description="shell command redirects or tees into a steering path",
        decision_class=DecisionClass.C,
        match=_rule_shell_steering_write,
    ),
    Rule(
        id="exec-later-write",
        description="action writes a path that runs code later (.git, shell rc, .envrc, editor)",
        decision_class=DecisionClass.C,
        match=_rule_exec_later_write,
    ),
    Rule(
        id="shell-exec-later-write",
        description="shell command redirects or tees into a path that runs code later",
        decision_class=DecisionClass.C,
        match=_rule_shell_exec_later_write,
    ),
    Rule(
        id="cap-exceeded",
        description="a declared spend/wall-clock/iteration cap is exceeded",
        decision_class=DecisionClass.C,
        match=_rule_cap_exceeded,
    ),
    # "never" sits with the refusals, ahead of every A grant: a tool that
    # declares it must not slip to auto via a specific grant like
    # in-workspace-edit (TD-4808; the C-before-A invariant test enforces
    # this placement).
    Rule(
        id="side-effect-never",
        description="tool declares side_effect_class 'never'",
        decision_class=DecisionClass.C,
        match=lambda req, _b: req.side_effect_class == "never",
    ),
    Rule(
        id="path-outside-writable",
        description="action writes in-workspace but outside writable_paths",
        decision_class=DecisionClass.C,
        match=_rule_outside_writable,
    ),
    Rule(
        id="shell-floor",
        description="shell commands always require approval (never model-granted A)",
        decision_class=DecisionClass.B,
        match=_rule_shell_floor,
    ),
    # Before every A grant: an MCP tool that never declared path/host
    # metadata must not be laundered into A by in-workspace-edit (if
    # writes were smuggled onto the request) or by the worker tier
    # (TD-4402).  C rules above still win — never, caps, new hosts.
    Rule(
        id="mcp-undeclared-fields",
        description="MCP tool with no declared path or host metadata — never model-granted A",
        decision_class=DecisionClass.B,
        match=_rule_mcp_undeclared_fields,
    ),
    Rule(
        id="memory-file-write",
        description="action writes a memory file (.tst/memory)",
        decision_class=DecisionClass.A,
        match=_rule_memory_write,
    ),
    Rule(
        id="in-workspace-edit",
        description="in-workspace source edit within writable_paths",
        decision_class=DecisionClass.A,
        match=_rule_in_workspace_edit,
    ),
    Rule(
        id="builtin-read",
        description="built-in read (fs_read, fs_list, fs_grep) inside the workspace",
        decision_class=DecisionClass.A,
        match=_rule_builtin_read,
    ),
    Rule(
        id="shell-read-only",
        description="shell command on the static read-only list",
        decision_class=DecisionClass.A,
        match=_rule_shell_read_only,
        allowed_by=_read_only_entries,
    ),
    Rule(
        id="desktop-capture",
        description="desktop capture cannot actuate",
        decision_class=DecisionClass.A,
        match=_rule_desktop_capture,
    ),
    Rule(
        id="desktop-actuation",
        description="desktop actuation requires approval",
        decision_class=DecisionClass.B,
        match=_rule_desktop_actuation,
    ),
    Rule(
        id="send-file-in-workspace",
        description="send_file of in-workspace files",
        decision_class=DecisionClass.A,
        match=_rule_send_file,
    ),
    Rule(
        id="steering-proposal",
        description="propose_steering_edit parks a diff for the user; writes nothing",
        decision_class=DecisionClass.A,
        match=_rule_steering_proposal,
    ),
    Rule(
        id="consult",
        description="consult asks the brain or validator a question; writes nothing",
        decision_class=DecisionClass.A,
        match=_rule_consult,
    ),
    # GitHub reads have no path or host metadata. A host on the request
    # would be Class C under the default network deny, and a path would
    # be Class A via in-workspace-edit. The grant is the tool name.
    Rule(
        id="github-read",
        description="GitHub read tools are class A",
        decision_class=DecisionClass.A,
        match=lambda req, _b: req.tool_name in GITHUB_READ_TOOLS,
    ),
    # The ask floor is terminal by design (TD-4808): every specific grant
    # above — memory writes, in-workspace edits, desktop capture — keeps
    # its deliberate class, and the floor only catches calls no rule spoke
    # for.  Before this, ``side_effect_class`` was advisory metadata the
    # classifier never read.
    Rule(
        id="side-effect-ask-floor",
        description="tool declares side_effect_class 'ask' — never model-granted A",
        decision_class=DecisionClass.B,
        match=lambda req, _b: req.side_effect_class == "ask",
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
                allowed = rule.allowed_by(request, self.boundary) if rule.allowed_by else ()
                named = f" ({', '.join(allowed)})" if allowed else ""
                return Classification(
                    decision_class=rule.decision_class,
                    rule=rule,
                    reason=f"[{rule.id}] {rule.description}{named}",
                    allowed_by=allowed,
                )
        return Classification(decision_class=None, rule=None, reason="no rule fired")
