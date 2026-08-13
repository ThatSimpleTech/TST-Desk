"""Steering assembly — resolve, read, and concatenate steering files.

Takes the discovered sources (``SteeringFileResolver``) and produces the
single steering block consumed by the prompt assembler (TD-305).
Each file is wrapped in a provenance comment naming its source so the
model knows where a rule came from (spec §4.1).

The primary public API is ``ContextAssembler.assemble()``, which runs
filesystem I/O in a worker thread via ``asyncio.to_thread`` so the
event loop is never blocked (AGENTS.md §6).  ``assemble_sync()`` is
available for synchronous contexts (tests, CLI).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..logging import get_logger
from .discover import Precedence, SteeringFileResolver, SteeringSource

log = get_logger("tstd.context")


@dataclass(frozen=True)
class ResolvedSource:
    """A steering source with its content read from disk.

    Attributes:
        path: Absolute path of the steering file.
        precedence: Precedence level.
        content: Raw file contents decoded as UTF-8.
        subtree: Workspace-relative subtree for nested files, else None.
        is_fallback: ``True`` when this source is a ``CLAUDE.md`` used
            because ``AGENTS.md`` is absent at the same path.
        shadowed_path: When this source is an ``AGENTS.md`` that
            outranks a present ``CLAUDE.md`` at the same location, this
            holds the path of the shadowed ``CLAUDE.md``.
    """

    path: Path
    precedence: Precedence
    content: str
    subtree: str | None = None
    is_fallback: bool = False
    shadowed_path: Path | None = None


@dataclass(frozen=True)
class AssembledSteering:
    """The resolved steering block plus per-source details.

    Attributes:
        block: Concatenated steering content with provenance comments
            naming each source file.
        sources: Every resolved source, lowest → highest precedence.
    """

    block: str
    sources: list[ResolvedSource]


class ContextAssembler:
    """Resolves and concatenates steering files into one block.

    Files are concatenated lowest → highest precedence so later
    content overrides earlier content when rules conflict.
    """

    def __init__(self, resolver: SteeringFileResolver | None = None) -> None:
        self._resolver = resolver or SteeringFileResolver()

    async def assemble(self, workspace_path: str | Path) -> AssembledSteering:
        """Resolve and assemble steering for *workspace_path*.

        Runs the filesystem work in a worker thread so the event loop
        is never blocked (AGENTS.md §6).
        """
        return await asyncio.to_thread(self.assemble_sync, workspace_path)

    def assemble_sync(self, workspace_path: str | Path) -> AssembledSteering:
        """Synchronous variant of :meth:`assemble` (tests, CLI)."""
        sources = self._resolver.resolve(workspace_path)
        resolved: list[ResolvedSource] = []
        parts: list[str] = []
        for source in sources:
            content = self._read(source.path)
            if content is None:
                continue  # missing or unreadable → not an error
            resolved.append(
                ResolvedSource(
                    path=source.path,
                    precedence=source.precedence,
                    content=content,
                    subtree=source.subtree,
                    is_fallback=source.is_fallback,
                    shadowed_path=source.shadowed_path,
                )
            )
            parts.append(self._render(source, content))
        return AssembledSteering(block="\n".join(parts), sources=resolved)

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
        return f"<!-- from: {source.path} {''.join(parts_list)} -->\n{content}"
