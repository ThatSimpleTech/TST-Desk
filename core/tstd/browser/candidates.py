"""Judgment-based candidate selection for the browser loop (TD-711, dev build).

Select-instead-of-generate: *code* extracts the candidate elements, the
configured ``JudgmentBackend`` picks the intended one, and code acts.
The payload is a fixed schema — index, tag, role, accessible name, and
bounding box per candidate, plus the target phrase.  No page text, HTML,
cookies, or URL crosses the boundary; the schema is the isolation.

Dev scope: candidates come from ``normalize_hit``-shaped node dicts, so
the mechanism is driver-agnostic and testable against the mock.  Wiring
a full DOM extraction into the Playwright driver is the hardening path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..autonomy.judgment import JudgmentBackend, JudgmentKind, JudgmentQuestion
from ..logging import get_logger

log = get_logger("tstd.candidates")

# Accessible-name attribute keys, in preference order across AX/ARIA/DOM.
_NAME_KEYS = ("AXTitle", "aria-label", "title", "id", "AXIdentifier", "data-testid")

# The no-match outcome: the model cannot choose an omitted value, and a
# page may genuinely lack the target — an explicit out beats a forced pick.
NO_MATCH = "none"

_SELECT_INSTRUCTION = (
    "Pick the candidate element that best matches the target phrase. The "
    "candidates are indexed UI elements with their accessible names and "
    "bounding boxes. Answer with the candidate's index, or 'none' when no "
    "candidate matches the target."
)


@dataclass(frozen=True)
class Candidate:
    """One selectable element, reduced to the isolation schema."""

    index: int
    tag: str
    role: str
    name: str
    box: tuple[float, float, float, float]


def candidate_from_node(node: Mapping[str, Any], index: int) -> Candidate | None:
    """Build a :class:`Candidate` from a ``normalize_hit``-shaped node.

    A top-level ``name`` (the extractor's best accessible-name computation)
    wins; otherwise the attribute preference order applies.  ``None`` when
    the node carries neither a role nor any name — there is nothing a
    judgment could match a phrase against.
    """
    role = node.get("role")
    role = role if isinstance(role, str) else ""
    name = ""
    direct = node.get("name")
    if isinstance(direct, str) and direct.strip():
        name = direct.strip()
    attributes = node.get("attributes")
    if not name and isinstance(attributes, Mapping):
        for key in _NAME_KEYS:
            value = attributes.get(key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
    if not role and not name:
        return None
    tag = node.get("tag")
    tag = tag if isinstance(tag, str) else ""
    box_raw = node.get("box")
    box = (0.0, 0.0, 0.0, 0.0)
    if isinstance(box_raw, Mapping):
        box = (
            _as_float(box_raw.get("x")),
            _as_float(box_raw.get("y")),
            _as_float(box_raw.get("width")),
            _as_float(box_raw.get("height")),
        )
    return Candidate(index=index, tag=tag, role=role, name=name, box=box)


def render_candidates(candidates: list[Candidate], max_chars: int) -> str:
    """Bounded one-line-per-candidate state text for the judgment."""
    lines = [
        f"{c.index}: [{c.role or c.tag or 'element'}] {c.name!r} "
        f"at ({c.box[0]:g},{c.box[1]:g} {c.box[2]:g}x{c.box[3]:g})"
        for c in candidates
    ]
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


async def select_candidate(
    backend: JudgmentBackend,
    target: str,
    candidates: list[Candidate],
    *,
    threshold: float = 0.6,
    max_state_chars: int = 2000,
) -> int | None:
    """Pick the candidate index matching *target*, or ``None``.

    ``None`` covers every non-assertion: no candidates, a failed or
    low-confidence judgment, an explicit ``none``, or an out-of-set
    answer.  Callers fall back to today's brain-driven path — never a
    block.
    """
    if not candidates:
        return None
    options = (*[str(c.index) for c in candidates], NO_MATCH)
    question = JudgmentQuestion(
        kind=JudgmentKind.CHOICE,
        instructions=_SELECT_INSTRUCTION,
        state=(
            ("Target", target),
            ("Candidates", render_candidates(candidates, max_state_chars)),
        ),
        options=options,
        question_id="candidate-select",
    )
    judgment = await backend.judge(question)
    if not judgment.ok or judgment.confidence < threshold:
        return None
    if judgment.label == NO_MATCH:
        log.info("candidate selection: no match", extra={"extra_fields": {"target": target}})
        return None
    try:
        picked = int(judgment.label)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not any(c.index == picked for c in candidates):
        return None
    log.info(
        "candidate selection",
        extra={
            "extra_fields": {
                "target": target,
                "picked": picked,
                "confidence": judgment.confidence,
                "backend": judgment.backend,
            }
        },
    )
    return picked


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0
