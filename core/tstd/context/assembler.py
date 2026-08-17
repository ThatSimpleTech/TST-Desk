"""Steering assembly — resolve, read, and concatenate steering files.

Takes the discovered sources (``SteeringFileResolver``) and produces the
single steering block consumed by the prompt assembler (TD-305).
Each file is wrapped in a provenance comment naming its source so the
model knows where a rule came from (spec §4.1).

**Path-scoped rules (TD-503).**  ``.tst/rules/*.md`` files may carry YAML
frontmatter with an ``appliesTo`` array of glob patterns.  The assembler
accepts ``matched_paths`` — paths the session has touched — and excludes
rules whose globs match none of them.  Inactive rules are still present in
``sources`` (for the inspector) but absent from the ``block`` (for the
model).

**Frontmatter elsewhere (TD-510).**  Frontmatter is parsed at *every*
level, so the ``---`` delimiters and their YAML never reach the model —
instruction files arriving from other tools commonly open with a block.
Only ``.tst/rules/`` gives ``appliesTo`` meaning, though: at any other
level it is stripped and flagged for the inspector, never honoured.  A
steering file's precedence *is* its scope, and letting a workspace-root
``AGENTS.md`` scope itself out of the prompt would make the working
agreement conditional on which files a session happened to touch.

The primary public API is ``ContextAssembler.assemble()``, which runs
filesystem I/O in a worker thread via ``asyncio.to_thread`` so the
event loop is never blocked (AGENTS.md §6).  ``assemble_sync()`` is
available for synchronous contexts (tests, CLI).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..logging import get_logger
from .discover import Precedence, SteeringFileResolver, SteeringSource
from .frontmatter import parse_frontmatter
from .imports import ImportDirective, process_imports
from .tokens import HeuristicTokenCounter, TokenCount, TokenCounter

log = get_logger("tstd.context")

# Files longer than this get a soft warning: adherence drops on long
# steering files (spec §4.3, TD-506).
LINE_LIMIT = 200


@dataclass(frozen=True)
class ResolvedSource:
    """A steering source with its content read from disk.

    Attributes:
        path: Absolute path of the steering file.
        precedence: Precedence level.
        content: Raw file contents decoded as UTF-8, with any frontmatter
            block stripped (every level — TD-510).
        subtree: Workspace-relative subtree for nested files, else None.
        is_fallback: ``True`` when this source is a ``CLAUDE.md`` used
            because ``AGENTS.md`` is absent at the same path.
        shadowed_path: When this source is an ``AGENTS.md`` that
            outranks a present ``CLAUDE.md`` at the same location, this
            holds the path of the shadowed ``CLAUDE.md``.
        applies_to: Parsed ``appliesTo`` glob patterns from frontmatter
            (rule files only), or ``None`` for non-rule sources.
        active: ``True`` when this source is included in the assembled
            block.  Path-scoped rules that match no touched path are
            ``False``.
        imports: Resolved ``@path`` import directives within this
            source (TD-504).  Empty for sources with no imports or for
            inactive sources (their imports are not processed).
        token_count: Token count for this source's content with the
            counting method (TD-506).
        line_count: Number of lines in this source's content.
        warnings: Soft warnings about this source (e.g. exceeding the
            line limit), for the inspector (TD-506).
    """

    path: Path
    precedence: Precedence
    content: str
    subtree: str | None = None
    is_fallback: bool = False
    shadowed_path: Path | None = None
    applies_to: tuple[str, ...] | None = None
    active: bool = True
    imports: tuple[ImportDirective, ...] = ()
    token_count: TokenCount = field(default_factory=lambda: TokenCount(count=0, method=""))
    line_count: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssembledSteering:
    """The resolved steering block plus per-source details.

    Attributes:
        block: Concatenated steering content with provenance comments
            naming each source file.  Only active sources appear here.
        sources: Every resolved source, lowest → highest precedence.
            Inactive sources are included (for the inspector) but absent
            from the block.
        import_issues: Flat list of import warnings/errors (TD-504/505).
        pending_imports: External-import paths awaiting approval (TD-505),
            deduplicated and sorted for determinism.  The loop raises an
            approval request for each before the turn proceeds.
    """

    block: str
    sources: list[ResolvedSource]
    import_issues: tuple[str, ...] = ()
    total_tokens: TokenCount = field(default_factory=lambda: TokenCount(count=0, method=""))
    pending_imports: tuple[Path, ...] = ()


class ContextAssembler:
    """Resolves and concatenates steering files into one block.

    Files are concatenated lowest → highest precedence so later
    content overrides earlier content when rules conflict.
    """

    def __init__(
        self,
        resolver: SteeringFileResolver | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self._resolver = resolver or SteeringFileResolver()
        self._token_counter = token_counter or HeuristicTokenCounter()

    async def assemble(
        self,
        workspace_path: str | Path,
        matched_paths: set[str] | None = None,
        source_filter: Callable[[SteeringSource], bool] | None = None,
        approved_imports: frozenset[Path] = frozenset(),
        denied_imports: frozenset[Path] = frozenset(),
    ) -> AssembledSteering:
        """Resolve and assemble steering for *workspace_path*.

        Args:
            workspace_path: Path to the workspace root.
            matched_paths: Set of workspace-relative paths the session
                has touched.  Path-scoped rules whose globs match none
                of these are excluded from the block.
            source_filter: Optional predicate that selects which
                steering sources to include in the block and in the
                ``sources`` list.  Sources that fail the filter are
                skipped entirely (no import processing, no rendering).
                Used by the validator tier (TD-508) to select only
                standards/conventions rules.
            approved_imports: External-import paths the user has approved
                for this workspace (TD-505).  Passed through to import
                resolution.
            denied_imports: External-import paths denied this session
                (TD-505).  Passed through to import resolution.

        Runs the filesystem work in a worker thread so the event loop
        is never blocked (AGENTS.md §6).
        """
        return await asyncio.to_thread(
            self.assemble_sync,
            workspace_path,
            matched_paths,
            source_filter,
            approved_imports,
            denied_imports,
        )

    def assemble_sync(
        self,
        workspace_path: str | Path,
        matched_paths: set[str] | None = None,
        source_filter: Callable[[SteeringSource], bool] | None = None,
        approved_imports: frozenset[Path] = frozenset(),
        denied_imports: frozenset[Path] = frozenset(),
    ) -> AssembledSteering:
        """Synchronous variant of :meth:`assemble` (tests, CLI)."""
        sources = self._resolver.resolve(workspace_path)
        resolved: list[ResolvedSource] = []
        parts: list[str] = []
        all_issues: list[str] = []
        pending: set[Path] = set()
        workspace_root = Path(workspace_path).resolve()
        for source in sources:
            if source_filter is not None and not source_filter(source):
                continue  # not part of this tier's context (TD-508)
            content = self._read(source.path)
            if content is None:
                continue  # missing or unreadable → not an error

            # Parsed at every level so the delimiters and YAML never reach
            # the model, but honoured only for rules (TD-510).
            applies_to: tuple[str, ...] | None = None
            active = True
            ignored_applies_to = False
            metadata, body = parse_frontmatter(content)

            if source.precedence == Precedence.RULES:
                raw_applies_to = metadata.get("appliesTo", [])
                if isinstance(raw_applies_to, list) and raw_applies_to:
                    applies_to = tuple(str(p) for p in raw_applies_to)
                    if matched_paths is not None:
                        active = _any_path_matches(matched_paths, applies_to)
            elif "appliesTo" in metadata:
                # Dropping it silently would swap one silent failure for
                # another; the inspector says so instead.
                ignored_applies_to = True

            # Process imports only for active sources — an inactive
            # scoped rule's imports would otherwise produce spurious
            # warnings the user never asked to load.
            imports: tuple[ImportDirective, ...] = ()
            if active:
                body, imports, issues = process_imports(
                    body,
                    source.path,
                    home_dir=self._resolver.home_dir,
                    workspace_path=workspace_root,
                    approved=approved_imports,
                    denied=denied_imports,
                    pending=pending,
                )
                all_issues.extend(issues)

            line_count = body.count("\n") + 1
            resolved.append(
                ResolvedSource(
                    path=source.path,
                    precedence=source.precedence,
                    content=body,
                    subtree=source.subtree,
                    is_fallback=source.is_fallback,
                    shadowed_path=source.shadowed_path,
                    applies_to=applies_to,
                    active=active,
                    imports=imports,
                    token_count=self._token_counter.count(body),
                    line_count=line_count,
                    warnings=self._warnings(line_count, ignored_applies_to),
                )
            )
            if active:
                parts.append(self._render(source, body))
        return AssembledSteering(
            block="\n".join(parts),
            sources=resolved,
            import_issues=tuple(all_issues),
            total_tokens=self._sum_tokens(resolved),
            pending_imports=tuple(sorted(pending)),
        )

    @staticmethod
    def _warnings(line_count: int, ignored_applies_to: bool) -> tuple[str, ...]:
        """Soft warnings about one source, for the inspector (TD-506, TD-510).

        Both texts end by naming the authoring guide, and the guide quotes
        them back verbatim — rewording either here fails the doc suite
        until that page catches up.
        """
        warnings: list[str] = []
        if line_count > LINE_LIMIT:
            warnings.append(
                f"file exceeds {LINE_LIMIT} lines; "
                f"long files measurably reduce adherence — "
                f"see the steering authoring guide"
            )
        if ignored_applies_to:
            warnings.append(
                "appliesTo only scopes rules in .tst/rules/; "
                "it was stripped from this file and had no effect — "
                "see the steering authoring guide"
            )
        return tuple(warnings)

    @staticmethod
    def _sum_tokens(sources: list[ResolvedSource]) -> TokenCount:
        """Total tokens across active sources, with the counting method."""
        active = [s for s in sources if s.active]
        if not active:
            return TokenCount(count=0, method="")
        method = active[0].token_count.method
        total = sum(s.token_count.count for s in active)
        return TokenCount(count=total, method=method)

    @staticmethod
    def _read(path: Path) -> str | None:
        """Read a steering file, or return None on failure.

        Missing files, permission errors, and non-UTF-8 content all
        return None — they are not errors (acceptance criterion).
        """
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            log.warning(
                "steering file unreadable, skipped",
                extra={"extra_fields": {"path": str(path)}},
            )
            return None
        except UnicodeDecodeError:
            log.warning(
                "steering file not valid UTF-8, skipped",
                extra={"extra_fields": {"path": str(path)}},
            )
            return None

    @staticmethod
    def _render(source: SteeringSource, content: str) -> str:
        """Wrap one source in its provenance comment.

        Returns a string like::

            <!-- from: /path/AGENTS.md (workspace) -->
            <content>

        When the source is a ``CLAUDE.md`` fallback, the label becomes
        e.g. ``(workspace, claude fallback)``.
        """
        parts_list: list[str] = [f"({source.precedence.label}"]
        if source.is_fallback:
            parts_list.append(", claude fallback")
        if source.subtree is not None:
            parts_list.append(f": {source.subtree}")
        parts_list.append(")")
        # POSIX separators always: provenance is prompt text, and OS-native
        # separators would make the cache prefix differ across platforms.
        return f"<!-- from: {source.path.as_posix()} {''.join(parts_list)} -->\n{content}"


# ── Glob matching ──────────────────────────────────────────────────────────


def _glob_to_re(pattern: str) -> re.Pattern[str]:
    """Convert a glob pattern to a compiled regex.

    Supports ``*`` (any chars except ``/``), ``**`` (any chars including
    ``/``), ``?`` (single char except ``/``), and ``[seq]`` / ``[!seq]``
    character classes.

    ``**/`` is translated as a unit to ``(?:.*/)?`` — zero or more whole
    path segments — rather than as ``**`` with the separator dropped.
    That distinction is what anchors the rest of the pattern to a segment
    boundary: ``.*config\\.py`` matches ``oldconfig.py``, while
    ``(?:.*/)?config\\.py`` does not (TD-511).
    """
    regex_parts: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                # `**/` spans whole segments or none at all.  Consuming
                # the separator into the group is what anchors whatever
                # follows to a segment boundary — translating `**` alone
                # and dropping the `/` let `**/config.py` match
                # `oldconfig.py` (TD-511).
                regex_parts.append("(?:.*/)?")
                i += 1
            else:
                # A trailing `**` takes everything below it.
                regex_parts.append(".*")
        elif c == "*":
            regex_parts.append("[^/]*")
            i += 1
        elif c == "?":
            regex_parts.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            negated = False
            if j < len(pattern) and pattern[j] in ("!", "^"):
                negated = True
                j += 1
            cls_parts: list[str] = ["[^" if negated else "["]
            while j < len(pattern) and pattern[j] != "]":
                ch = pattern[j]
                if ch == "\\":
                    cls_parts.append("\\\\")
                else:
                    cls_parts.append(ch)  # not escaped — ranges like a-z work
                j += 1
            if j < len(pattern):
                cls_parts.append("]")
                i = j + 1
            else:
                # No closing bracket — treat as literal
                regex_parts.append(re.escape(c))
                i += 1
                continue
            regex_parts.append("".join(cls_parts))
        else:
            regex_parts.append(re.escape(c))
            i += 1

    return re.compile(f"^{''.join(regex_parts)}$")


def _any_path_matches(paths: set[str], patterns: tuple[str, ...]) -> bool:
    """Return True if any *path* matches any of the *patterns*.

    Patterns without a ``/`` are matched at any depth (like ``.gitignore``
    convention).  Patterns with a ``/`` match the full path from the
    workspace root.
    """
    for path in paths:
        posix = path.replace("\\", "/")  # normalise Windows paths
        for pattern in patterns:
            if _path_matches_glob(posix, pattern):
                return True
    return False


def _path_matches_glob(path: str, pattern: str) -> bool:
    """Check if a single *path* matches a single *pattern*.

    A pattern with no ``/`` is rewritten to ``**/<pattern>`` and so is
    matched against the whole basename at any depth — ``config.py``
    matches ``pkg/config.py`` but not ``oldconfig.py``.  The two
    spellings are deliberately synonyms: both anchor.
    """
    if "/" not in pattern and pattern != "**":
        # Match at any depth — like .gitignore convention.
        pattern = f"**/{pattern}"
    regex = _glob_to_re(pattern)
    return bool(regex.match(path))
