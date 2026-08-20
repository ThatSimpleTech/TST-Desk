"""Heading-match memory loader (TD-2201).

Spec §5 mechanic 1: load ``MEMORY.md`` plus any topic file whose heading
tokens overlap the user task. File-level selection, no embeddings. An
empty or missing ``.tst/memory/`` returns nothing so the prompt keeps
the placeholder.

This is the M4 floor. Embeddings (TD-2202-2204) may rank on top; they
must fall back here, not replace it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..logging import get_logger
from ..memory_store import memory_dir
from .imports import _iter_lines

log = get_logger("tstd.memory_loader")

INDEX_NAME = "MEMORY.md"
_INDEX_KEY = "MEMORY.MD"
_STEERING_BASENAMES = frozenset({"AGENTS.MD", "CLAUDE.MD"})

# Tokens shorter than this are noise ("a", "to", "is") and would make
# every file match every task. Recorded in DECISIONS.md (TD-2201).
_MIN_TOKEN_LEN = 3
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "are",
        "was",
        "were",
        "been",
        "have",
        "has",
        "not",
        "but",
        "you",
        "your",
        "into",
        "than",
        "then",
        "them",
        "they",
        "their",
        "what",
        "when",
        "how",
        "why",
        "who",
        "which",
        "can",
        "will",
        "just",
        "about",
    }
)
_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

MemoryReason = Literal["always-index", "heading", "embedding"]


@dataclass(frozen=True)
class MemoryFile:
    """One selected memory file and why it was chosen."""

    path: Path
    relative: Path
    reason: MemoryReason
    text: str


@dataclass(frozen=True)
class MemoryLoad:
    """The heading-match selection for one task.

    ``block`` is ``None`` when nothing loaded so the assembler keeps
    ``MEMORY_PLACEHOLDER``.
    """

    files: tuple[MemoryFile, ...]

    @property
    def block(self) -> str | None:
        if not self.files:
            return None
        parts = [
            f"<!-- memory: {item.relative.as_posix()} ({item.reason}) -->\n{item.text}"
            for item in self.files
        ]
        return "\n\n".join(parts)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.path.name for item in self.files)


def tokenize(text: str) -> frozenset[str]:
    """Lowercased alphanumeric tokens, minus stopwords and short crumbs."""
    return frozenset(
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) >= _MIN_TOKEN_LEN and token not in _STOPWORDS
    )


def heading_tokens(markdown: str) -> frozenset[str]:
    """Union of tokens from ATX headings outside fenced code blocks."""
    tokens: set[str] = set()
    for _, line, in_fence in _iter_lines(markdown):
        if in_fence:
            continue
        match = _ATX_RE.match(line)
        if match is None:
            continue
        tokens.update(tokenize(match.group(2)))
    return frozenset(tokens)


def headings_overlap_task(markdown: str, task: str) -> bool:
    """Whether any heading token also appears in the task."""
    return bool(heading_tokens(markdown) & tokenize(task))


@dataclass(frozen=True)
class MemoryCandidate:
    """A readable memory file before ranking or heading-match."""

    path: Path
    relative: Path
    text: str
    is_index: bool


def discover_memory_files(workspace: str | Path) -> tuple[MemoryCandidate, ...]:
    """Every readable ``.tst/memory/*.md``, index first, then sorted names.

    Does not filter by heading. Ranking (TD-2203) and heading-match
    (TD-2201) both start from this list. Escapes and unreadable files
    are skipped the same way as :func:`load_memory_for_task`.
    """
    workspace_root = Path(workspace).resolve()
    root = memory_dir(workspace_root)
    try:
        if not root.is_dir():
            return ()
        root = root.resolve()
    except OSError as exc:
        log.warning(
            "memory directory unreadable, skipped",
            extra={"extra_fields": {"path": str(root), "error": str(exc)}},
        )
        return ()

    index: MemoryCandidate | None = None
    topics: list[MemoryCandidate] = []
    for path in sorted(root.glob("*.md")):
        found = _read_candidate(path, root, workspace_root)
        if found is None:
            continue
        if found.is_index:
            index = found
        else:
            topics.append(found)
    if index is None:
        return tuple(topics)
    return (index, *topics)


def load_memory_for_task(workspace: str | Path, task: str) -> MemoryLoad:
    """Select ``MEMORY.md`` plus topic files whose headings overlap *task*.

    Missing or empty ``.tst/memory/`` returns an empty load. Unreadable
    files and paths that escape the memory directory are skipped. The
    result is ordered: index first, then remaining names sorted.
    """
    selected: list[MemoryFile] = []
    for candidate in discover_memory_files(workspace):
        if candidate.is_index:
            selected.append(_to_file(candidate, "always-index"))
            continue
        if headings_overlap_task(candidate.text, task):
            selected.append(_to_file(candidate, "heading"))
    return MemoryLoad(tuple(selected))


def _read_candidate(
    path: Path,
    memory_root: Path,
    workspace_root: Path,
) -> MemoryCandidate | None:
    key = path.name.upper()
    if key in _STEERING_BASENAMES:
        return None
    try:
        resolved = path.resolve()
    except OSError as exc:
        log.warning(
            "memory file unreadable, skipped",
            extra={"extra_fields": {"path": str(path), "error": str(exc)}},
        )
        return None
    if not resolved.is_file() or not _is_under(resolved, memory_root):
        log.warning(
            "memory path escaped .tst/memory, skipped",
            extra={"extra_fields": {"path": str(path), "resolved": str(resolved)}},
        )
        return None
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "memory file unreadable, skipped",
            extra={"extra_fields": {"path": str(path), "error": str(exc)}},
        )
        return None
    except UnicodeDecodeError:
        log.warning(
            "memory file not valid UTF-8, skipped",
            extra={"extra_fields": {"path": str(path)}},
        )
        return None
    try:
        relative = resolved.relative_to(workspace_root)
    except ValueError:
        relative = Path(".tst") / "memory" / path.name
    return MemoryCandidate(
        path=resolved,
        relative=relative,
        text=text,
        is_index=key == _INDEX_KEY,
    )


def _to_file(candidate: MemoryCandidate, reason: MemoryReason) -> MemoryFile:
    return MemoryFile(
        path=candidate.path,
        relative=candidate.relative,
        reason=reason,
        text=candidate.text,
    )


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
