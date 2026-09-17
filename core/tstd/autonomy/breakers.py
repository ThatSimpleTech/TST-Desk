"""Circuit breakers for unattended runs (TD-4203, spec §12.7).

These are fault reports, not permission requests: a trip sets
``session.autonomy_stop_reason`` and the scheduler notifies. Interactive
sessions never record and never trip. Spend/wall-clock caps and Class C
already stop elsewhere — this module does not rewrite those reasons.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..logging import get_logger
from .judgment import JudgmentBackend, JudgmentKind, JudgmentQuestion
from .verify import WRITE_TOOLS

log = get_logger("tstd.breakers")

if TYPE_CHECKING:
    from ..session import Session

TESTS_RED = "breaker:tests_red"
FILE_THRASH = "breaker:file_thrash"
NO_DOD_PROGRESS = "breaker:no_dod_progress"
TOOL_LOOP = "breaker:tool_loop"
SEMANTIC_NO_PROGRESS = "breaker:no_semantic_progress"

DEFAULT_TESTS_RED_N = 3
DEFAULT_FILE_THRASH_N = 5
DEFAULT_NO_DOD_PROGRESS_N = 3
DEFAULT_TOOL_LOOP_N = 3
DEFAULT_SEMANTIC_NO_PROGRESS_N = 3

_TESTS_RED_RE = re.compile(r"tests\s+red\s+for\s+(\d+)", re.IGNORECASE)
_FILE_THRASH_RE = re.compile(r"same\s+file\s+(?:modified|thrashed)\s+(\d+)", re.IGNORECASE)
_NO_PROGRESS_RE = re.compile(r"no\s+(?:measurable\s+)?progress.*?(\d+)", re.IGNORECASE)
_TOOL_LOOP_RE = re.compile(
    r"(?:identical\s+tool|tool[- ]call\s+loop|tool\s+loop).*?(\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BreakerLimits:
    """N for each breaker. Charter ``stop_conditions`` override defaults."""

    tests_red: int
    file_thrash: int
    no_dod_progress: int
    tool_loop: int


def limits_for(session: Session) -> BreakerLimits:
    """Resolve N from charter prose, else the spec/charter defaults."""
    tests_red = DEFAULT_TESTS_RED_N
    file_thrash = DEFAULT_FILE_THRASH_N
    no_dod_progress = DEFAULT_NO_DOD_PROGRESS_N
    tool_loop = DEFAULT_TOOL_LOOP_N
    charter = session.charter
    if charter is None:
        return BreakerLimits(tests_red, file_thrash, no_dod_progress, tool_loop)
    for line in charter.stop_conditions:
        matched = _TESTS_RED_RE.search(line)
        if matched:
            tests_red = max(1, int(matched.group(1)))
        matched = _FILE_THRASH_RE.search(line)
        if matched:
            file_thrash = max(1, int(matched.group(1)))
        matched = _NO_PROGRESS_RE.search(line)
        if matched:
            no_dod_progress = max(1, int(matched.group(1)))
        matched = _TOOL_LOOP_RE.search(line)
        if matched:
            tool_loop = max(1, int(matched.group(1)))
    return BreakerLimits(tests_red, file_thrash, no_dod_progress, tool_loop)


def record_tool_round(session: Session, items: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
    """Fingerprint this dispatch batch; increment the identical-round streak."""
    if not session.autonomy or not items:
        return
    fingerprint = tuple((name, _canonical_args(args)) for name, args in items)
    if fingerprint == session.last_tool_fingerprint:
        session.tool_loop_streak += 1
    else:
        session.tool_loop_streak = 1
    session.last_tool_fingerprint = fingerprint


def record_file_write(session: Session, path: str) -> None:
    """Count a successful workspace write toward the thrash breaker."""
    if not session.autonomy:
        return
    rel = _relative_path(session, path)
    if rel is None:
        return
    session.file_write_counts[rel] = session.file_write_counts.get(rel, 0) + 1


def record_autonomy_round(
    session: Session,
    items: Sequence[tuple[str, str, Mapping[str, Any]]],
    results: Sequence[Any],
) -> None:
    """Loop hook: one dispatch batch's fingerprint and successful writes."""
    if not session.autonomy:
        return
    record_tool_round(session, [(name, args) for _call_id, name, args in items])
    by_id = {item[0]: item for item in items}
    for result in results:
        if getattr(result, "status", None) != "success":
            continue
        name = getattr(result, "name", "")
        if name not in WRITE_TOOLS:
            continue
        item = by_id.get(getattr(result, "tool_call_id", ""))
        args: Mapping[str, Any] = item[2] if item is not None else {}
        path = args.get("path")
        if isinstance(path, str) and path.strip():
            record_file_write(session, path)


def maybe_trip(session: Session) -> str | None:
    """Return a ``breaker:`` reason after this turn, or ``None`` to continue.

    Updates DoD streaks from ``last_dod_poll`` once per autonomy turn.
    Interactive sessions always return ``None``.
    """
    if not session.autonomy:
        return None
    _ingest_dod_poll(session)
    limits = limits_for(session)
    if session.red_dod_streak >= limits.tests_red:
        return TESTS_RED
    if session.no_dod_progress_streak >= limits.no_dod_progress:
        return NO_DOD_PROGRESS
    if any(count >= limits.file_thrash for count in session.file_write_counts.values()):
        return FILE_THRASH
    if session.tool_loop_streak >= limits.tool_loop:
        return TOOL_LOOP
    return None


def _ingest_dod_poll(session: Session) -> None:
    """Fold this turn's poll into the red / no-progress / thrash counters."""
    if session._breaker_seen_turn == session.autonomy_turns:
        return
    session._breaker_seen_turn = session.autonomy_turns
    poll = session.last_dod_poll
    if poll is None:
        return
    greens = _green_count(poll)
    if getattr(poll, "all_green", False):
        session.red_dod_streak = 0
        session.no_dod_progress_streak = 0
        session.file_write_counts.clear()
    else:
        if greens == 0:
            session.red_dod_streak += 1
        else:
            session.red_dod_streak = 0
        previous = session.last_dod_green_count
        if previous is not None and greens > previous:
            session.no_dod_progress_streak = 1
            session.file_write_counts.clear()
        else:
            session.no_dod_progress_streak += 1
    session.last_dod_green_count = greens


def _green_count(poll: object) -> int:
    results = getattr(poll, "results", ())
    return sum(1 for item in results if getattr(item, "green", False))


# ── Semantic no-progress breaker (TD-710, dev build) ───────────────────
#
# The syntactic four cannot see a run that keeps *changing* things —
# clicking, scrolling, navigating — without approaching the objective.
# This breaker asks the judgment seam, once per autonomy turn, whether
# the recent activity made measurable progress.  It is inert without a
# backend, on a failed or low-confidence judgment, and for interactive
# sessions: the fallback is exactly today's syntactic-only behavior.

_SEMANTIC_INSTRUCTION = (
    "An unattended agent run is in progress. Judge whether the recent activity "
    "made measurable progress toward the objective. Answer no when the run is "
    "spinning — acting repeatedly without approaching the objective."
)


async def maybe_trip_semantic(
    session: Session,
    backend: JudgmentBackend | None,
    *,
    threshold: float = 0.6,
    limit: int = DEFAULT_SEMANTIC_NO_PROGRESS_N,
) -> str | None:
    """Trip ``breaker:no_semantic_progress`` after *limit* no-progress rounds.

    Returns ``None`` to continue.  Never trips on an error — a judgment
    that cannot be made leaves the syntactic breakers to do their job.
    """
    if not session.autonomy or backend is None:
        return None
    charter = session.charter
    if charter is None:
        return None
    question = JudgmentQuestion(
        kind=JudgmentKind.NOUL,
        instructions=_SEMANTIC_INSTRUCTION,
        state=(
            ("Objective", charter.objective),
            ("Recent activity", _recent_activity(session)),
            ("Finished turns", str(session.autonomy_turns)),
        ),
        question_id="semantic-progress",
    )
    try:
        judgment = await backend.judge(question)
    except Exception:  # connectors should not raise; inert if one does
        log.exception("semantic breaker judgment raised; breaker stays inert")
        return None
    if not judgment.ok or judgment.confidence < threshold:
        return None
    if judgment.label == "no":
        session.semantic_no_progress_streak += 1
    else:
        session.semantic_no_progress_streak = 0
    if session.semantic_no_progress_streak >= limit:
        return SEMANTIC_NO_PROGRESS
    return None


def _recent_activity(session: Session) -> str:
    """A compact text summary of the run's recent motion, for the judgment."""
    parts: list[str] = []
    if session.last_tool_fingerprint:
        names = ", ".join(name for name, _ in session.last_tool_fingerprint)
        parts.append(f"last tool batch: {names} (repeated {session.tool_loop_streak}x)")
    if session.file_write_counts:
        top = sorted(session.file_write_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        parts.append("files written: " + ", ".join(f"{path} ({n}x)" for path, n in top))
    poll = session.last_dod_poll
    if poll is not None:
        parts.append(f"definition-of-done greens: {_green_count(poll)}")
    return "; ".join(parts) if parts else "(no tool activity yet)"


def _canonical_args(args: Mapping[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, default=str)


def _relative_path(session: Session, raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        root = Path(session.workspace_path).resolve()
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return None
    return path.as_posix()
