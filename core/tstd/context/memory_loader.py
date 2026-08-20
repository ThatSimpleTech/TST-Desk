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

MemoryReason = Literal["always-index", "heading"]


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


def load_memory_for_task(workspace: str | Path, task: str) -> MemoryLoad:
    """Select ``MEMORY.md`` plus topic files whose headings overlap *task*.

    Missing or empty ``.tst/memory/`` returns an empty load. Unreadable
    files and paths that escape the memory directory are skipped. The
    result is ordered: index first, then remaining names sorted.
    """
    workspace_root = Path(workspace).resolve()
    root = memory_dir(workspace_root)
    try:
        if not root.is_dir():
            return MemoryLoad(())
        root = root.resolve()
    except OSError as exc:
        log.warning(
            "memory directory unreadable, skipped",
            extra={"extra_fields": {"path": str(root), "error": str(exc)}},
        )
        return MemoryLoad(())

    index: MemoryFile | None = None
    topics: list[MemoryFile] = []
    for path in sorted(root.glob("*.md")):
        selected = _consider(path, root, workspace_root, task)
        if selected is None:
            continue
        if selected.reason == "always-index":
            index = selected
        else:
            topics.append(selected)

    files: list[MemoryFile] = []
    if index is not None:
        files.append(index)
    files.extend(topics)
    return MemoryLoad(tuple(files))


def _consider(
    path: Path,
    memory_root: Path,
    workspace_root: Path,
    task: str,
) -> MemoryFile | None:
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

    reason: MemoryReason
    if key == _INDEX_KEY:
        reason = "always-index"
    elif headings_overlap_task(text, task):
        reason = "heading"
    else:
        return None

    try:
        relative = resolved.relative_to(workspace_root)
    except ValueError:
        relative = Path(".tst") / "memory" / path.name
    return MemoryFile(path=resolved, relative=relative, reason=reason, text=text)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
