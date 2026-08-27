"""Import resolution for steering files.

Implements ``@path/to/file.md`` inline imports (spec §4.2):

* Relative paths resolve against the importing file's directory.
* Absolute paths and ``~`` expansion are supported.
* Maximum import depth is 4; exceeding it produces a clear error
  naming the chain.
* Cycles are detected and reported with the full cycle path.
* Import directives inside fenced code blocks (````` ``` ````) are
  not evaluated.  Inline code spans (`` ` ``) are excluded naturally
  by the line-anchored import regex.
* Missing files produce a warning and do not abort the session.
* Imported content carries provenance to its own file, not the
  importer's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Regex for import directives: a line starting with optional whitespace,
# ``@``, then a non-whitespace path, then optional trailing whitespace.
_IMPORT_RE = re.compile(r"^\s*@(?P<path>\S+)\s*$")

# Maximum import nesting depth (spec §4.2).
MAX_IMPORT_DEPTH = 4


@dataclass(frozen=True)
class ImportDirective:
    """A resolved import directive within a steering file.

    Attributes:
        path: Absolute path of the imported file.
        content: Rendered content (with provenance comment and nested
            imports inlined), or ``None`` if resolution failed.
        issue: Human-readable error/warning message set when resolution
            failed (missing file, cycle, depth exceeded), or ``None``
            when the import resolved successfully.
        depth: Import depth of this file (1 = top-level import).
        imports: Nested imports within this file, for the inspector
            (TD-1201).
    """

    path: Path
    content: str | None
    issue: str | None = None
    depth: int = 1
    imports: tuple[ImportDirective, ...] = ()


def _iter_lines(content: str) -> list[tuple[int, str, bool]]:
    """Yield ``(line_number, line, in_fence)`` for each line of *content*.

    *in_fence* is ``True`` for lines inside a triple-backtick fenced
    code block.  Both the fence lines and their content are marked as
    code, so import directives inside documentation code blocks are
    correctly excluded.
    """
    result: list[tuple[int, str, bool]] = []
    in_fence = False
    for i, line in enumerate(content.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            result.append((i, line, True))
        else:
            result.append((i, line, in_fence))
    return result


def _resolve_path(raw: str, base_dir: Path, home_dir: Path) -> Path:
    """Resolve an import path from a directive like ``@path/to/file.md``.

    * ``~`` or ``~/...`` — expanded via *home_dir* (test seam).
    * Absolute paths — used as-is.
    * Relative paths — resolved against *base_dir* (the importing file's
      directory).
    """
    if raw.startswith("~"):
        # Manual expansion using the injected home_dir (not the OS home).
        rest = raw[1:]  # "~" or "~/..."
        if rest.startswith("/"):
            rest = rest[1:]
        return (home_dir / rest).resolve()
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    return (base_dir / p).resolve()


def _is_outside(path: Path, workspace: Path) -> bool:
    """Return ``True`` when *path* is not within *workspace*.

    Both paths must already be absolute and resolved so the comparison is
    canonical (TD-602 orders every path check this way).
    """
    try:
        path.relative_to(workspace)
        return False
    except ValueError:
        return True


def _user_steering_root(home_dir: Path) -> Path:
    """``~/.tstdesk`` — personal-global steering tree (TD-4905)."""
    return (home_dir / ".tstdesk").resolve()


def _under_user_steering(path: Path, home_dir: Path) -> bool:
    """True when *path* lives under the personal-global steering directory."""
    try:
        path.resolve().relative_to(_user_steering_root(home_dir))
        return True
    except ValueError:
        return False


def _collect_issues(imports: tuple[ImportDirective, ...]) -> list[str]:
    """Flatten all issues from an import tree into a single list."""
    issues: list[str] = []
    for imp in imports:
        if imp.issue:
            issues.append(imp.issue)
        issues.extend(_collect_issues(imp.imports))
    return issues


def process_imports(
    content: str,
    source_path: Path,
    home_dir: Path,
    chain: tuple[Path, ...] = (),
    *,
    workspace_path: Path | None = None,
    approved: frozenset[Path] = frozenset(),
    denied: frozenset[Path] = frozenset(),
    pending: set[Path] | None = None,
) -> tuple[str, tuple[ImportDirective, ...], list[str]]:
    """Process import directives in *content*.

    Args:
        content: The file content to scan for imports (frontmatter
            already stripped, if applicable).
        source_path: Absolute path of the file containing *content*
            (used for relative-path resolution and provenance).
        home_dir: Home directory for ``~`` expansion.
        chain: Tuple of absolute paths currently in the import chain
            (for cycle detection).  Empty for the top-level call.
        workspace_path: Absolute, resolved workspace root.  When set,
            imports resolving outside it are gated (TD-505): approved
            paths are read, denied paths are omitted with a warning, and
            everything else is collected in *pending* and omitted until
            the loop resolves approval.  ``None`` disables the gate
            (backward-compatible with direct resolution tests).
        approved: Absolute paths of external imports the user has already
            approved for this workspace.
        denied: Absolute paths of external imports the user denied this
            session.  They are omitted and not re-prompted.
        pending: Mutable set that collects external-import paths awaiting
            approval.  Shared across recursive calls so a nested import
            discovered after an approval is still surfaced to the loop.

    Returns:
        ``(processed_content, imports, issues)``:
        *processed_content* is *content* with each ``@path`` directive
        replaced by the imported file's rendered content (or an error
        comment).  *imports* is the tree of resolved directives.
        *issues* is a flat list of all warning/error messages.
    """
    lines = _iter_lines(content)
    out_lines: list[str] = []
    import_list: list[ImportDirective] = []
    all_issues: list[str] = []
    if pending is None:
        pending = set()

    for _, line, in_fence in lines:
        if in_fence:
            out_lines.append(line)
            continue

        m = _IMPORT_RE.match(line)
        if not m:
            out_lines.append(line)
            continue

        # Found an import directive.
        raw_path = m.group("path")
        resolved_path = _resolve_path(raw_path, source_path.parent, home_dir)
        depth = len(chain) + 1

        # ── Cycle check ────────────────────────────────────────────
        if resolved_path in chain:
            cycle_path = " -> ".join(str(p.name) for p in (*chain, resolved_path))
            import_list.append(
                ImportDirective(
                    path=resolved_path,
                    content=None,
                    issue=f"import cycle detected: {cycle_path}",
                    depth=depth,
                )
            )
            all_issues.append(f"import cycle detected: {cycle_path}")
            out_lines.append(f"<!-- import cycle detected: {cycle_path} -->")
            continue

        # ── Depth check ────────────────────────────────────────────
        if depth > MAX_IMPORT_DEPTH:
            chain_names = " -> ".join(str(p.name) for p in chain)
            full_chain = f"{chain_names} -> {resolved_path.name}"
            import_list.append(
                ImportDirective(
                    path=resolved_path,
                    content=None,
                    issue=f"max import depth {MAX_IMPORT_DEPTH} exceeded: {full_chain}",
                    depth=depth,
                )
            )
            all_issues.append(f"max import depth {MAX_IMPORT_DEPTH} exceeded: {full_chain}")
            out_lines.append(f"<!-- max import depth {MAX_IMPORT_DEPTH} exceeded: {full_chain} -->")
            continue

        # ── External-import gate (TD-505) ───────────────────────────
        # An import resolving outside the workspace is an untrusted-file
        # read (Class C).  Approved paths read normally; denied paths are
        # omitted with a warning; anything else is omitted and collected
        # in *pending* for the loop to raise an approval request.  A
        # missing external file is "not found", not "awaiting approval" —
        # there is nothing to read, so nothing to approve.
        gated = (
            workspace_path is not None
            and _is_outside(resolved_path, workspace_path)
            and resolved_path not in approved
            and not (
                _under_user_steering(source_path, home_dir)
                and _under_user_steering(resolved_path, home_dir)
            )
        )
        if gated:
            if resolved_path in denied:
                issue = f"external import denied: {resolved_path}"
            elif not resolved_path.exists():
                issue = f"import file not found: {resolved_path}"
            else:
                pending.add(resolved_path)
                issue = f"external import awaiting approval: {resolved_path}"
            import_list.append(
                ImportDirective(path=resolved_path, content=None, issue=issue, depth=depth)
            )
            all_issues.append(issue)
            out_lines.append(f"<!-- {issue} -->")
            continue

        # ── Read the imported file ─────────────────────────────────
        if not resolved_path.exists():
            import_list.append(
                ImportDirective(
                    path=resolved_path,
                    content=None,
                    issue=f"import file not found: {resolved_path}",
                    depth=depth,
                )
            )
            all_issues.append(f"import file not found: {resolved_path}")
            out_lines.append(f"<!-- import file not found: {resolved_path} -->")
            continue

        try:
            imported_content = resolved_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            import_list.append(
                ImportDirective(
                    path=resolved_path,
                    content=None,
                    issue=f"import file unreadable: {resolved_path}",
                    depth=depth,
                )
            )
            all_issues.append(f"import file unreadable: {resolved_path}")
            out_lines.append(f"<!-- import file unreadable: {resolved_path.as_posix()} -->")
            continue

        # ── Recurse for nested imports ─────────────────────────────
        processed, nested, nested_issues = process_imports(
            imported_content,
            resolved_path,
            home_dir,
            (*chain, resolved_path),
            workspace_path=workspace_path,
            approved=approved,
            denied=denied,
            pending=pending,
        )
        all_issues.extend(nested_issues)

        rendered = f"<!-- from: {resolved_path.as_posix()} (imported) -->\n{processed}"
        import_list.append(
            ImportDirective(
                path=resolved_path,
                content=rendered,
                depth=depth,
                imports=nested,
            )
        )
        out_lines.append(rendered)

    return "\n".join(out_lines), tuple(import_list), all_issues
