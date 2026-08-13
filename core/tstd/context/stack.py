"""Build the ``instruction_stack`` protocol event from assembled steering.

TD-506: counts are available via ``get_instruction_stack``.  This module
builds the daemon event payload from an ``AssembledSteering``; the daemon
routes the request to it once session message routing lands (TD-305).
"""

from __future__ import annotations

from tstd.protocol import InstructionStack, InstructionStackEntry

from .assembler import AssembledSteering


def build_instruction_stack(
    session_id: str,
    steering: AssembledSteering,
    seq: int = 1,
) -> InstructionStack:
    """Build the ``instruction_stack`` event for *steering*.

    Each resolved source becomes an ``InstructionStackEntry`` carrying
    its path, precedence label, token count with method, warnings, and
    activity.  The event's totals cover active sources only — inactive
    rules are not in the prompt and cost nothing.
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
        )
        for source in steering.sources
    ]
    return InstructionStack(
        seq=seq,
        session_id=session_id,
        sources=entries,
        total_tokens=steering.total_tokens.count,
        token_method=steering.total_tokens.method,
    )
