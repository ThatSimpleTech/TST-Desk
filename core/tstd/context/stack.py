"""Build the ``instruction_stack`` protocol event from assembled steering.

TD-506: counts are available via ``get_instruction_stack``.  This module
builds the daemon event payload from an ``AssembledSteering``; the daemon
answers the query with it (TD-1201) and the loop pushes it again whenever
steering reloads mid-session (TD-509).
"""

from __future__ import annotations

from tstd.protocol import ImportedFile, InstructionStack, InstructionStackEntry

from .assembler import AssembledSteering
from .imports import ImportDirective


def _flatten_imports(directives: tuple[ImportDirective, ...]) -> list[ImportedFile]:
    """Flatten the import tree, keeping ``depth`` so the UI can re-nest it."""
    flat: list[ImportedFile] = []
    for directive in directives:
        flat.append(
            ImportedFile(
                path=str(directive.path),
                depth=directive.depth,
                issue=directive.issue,
            )
        )
        flat.extend(_flatten_imports(directive.imports))
    return flat


def build_instruction_stack(
    session_id: str,
    steering: AssembledSteering,
    seq: int = 1,
    *,
    last_cached_tokens: int | None = None,
) -> InstructionStack:
    """Build the ``instruction_stack`` event for *steering*.

    Each resolved source becomes an ``InstructionStackEntry`` carrying
    its path, precedence label, token count with method, warnings, and
    activity.  The event's totals cover active sources only — inactive
    rules are not in the prompt and cost nothing.  ``last_cached_tokens``
    is the most recent provider-observed cache figure (None before the
    first turn); the caller supplies it, this module never derives it.
    """
    entries = [
        InstructionStackEntry(
            path=str(source.path),
            precedence=source.precedence.label,
            active=source.active,
            tokens=source.token_count.count,
            token_method=source.token_count.method,
            warnings=list(source.warnings),
            subtree=source.subtree,
            is_fallback=source.is_fallback,
            shadowed_path=(str(source.shadowed_path) if source.shadowed_path is not None else None),
            applies_to=list(source.applies_to) if source.applies_to is not None else None,
            imports=_flatten_imports(source.imports),
        )
        for source in steering.sources
    ]
    return InstructionStack(
        seq=seq,
        session_id=session_id,
        sources=entries,
        total_tokens=steering.total_tokens.count,
        token_method=steering.total_tokens.method,
        last_cached_tokens=last_cached_tokens,
    )
