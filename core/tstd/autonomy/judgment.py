"""Provider-neutral judgment seam (TD-708, dev build).

A *judgment* is a typed answer to a narrow question over bounded text
state — the shape spec §12.2 designed for the classifier fallback and
that TD-709/TD-710/TD-711 reuse for computer-use verification, the
semantic breaker, and candidate selection.

The seam is the infrastructure; connectors are pluggable:

- ``WorkerChatJudgmentBackend`` is the **default connector** — it wraps
  the same worker-tier completion callable TD-703 already used, with a
  strict parse.  The product runs fully on it; no external judgments
  API is configured, present, or required.
- A typed-judgment API (the TypeSafe/Jev connector) is a separate,
  optional module selected by configuration — never named here.

Every connector fails closed: errors, timeouts, unparseable replies,
and unknown labels yield a ``Judgment`` with ``label=None``, and callers
map that to their safe default (B for the classifier, "unavailable" for
verification, inert for the breaker).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..logging import get_logger

log = get_logger("tstd.judgment")


class JudgmentKind(StrEnum):
    """The two judgment shapes the dev build uses.

    ``CHOICE`` picks one of a defined option set; ``NOUL`` is a yes/no
    probability, rendered as the fixed option pair ``("no", "yes")``.
    """

    CHOICE = "choice"
    NOUL = "noul"


NOUL_OPTIONS: tuple[str, ...] = ("no", "yes")


@dataclass(frozen=True)
class JudgmentQuestion:
    """One narrow judgment over bounded text state.

    ``state`` is ordered key/value text — the *only* thing a connector
    may send.  Callers bound each value (see ``max_state_chars`` on the
    features) so what leaves the machine is inspectable and capped.
    """

    kind: JudgmentKind
    instructions: str
    state: tuple[tuple[str, str], ...] = ()
    options: tuple[str, ...] = ()
    question_id: str = ""

    def resolved_options(self) -> tuple[str, ...]:
        """The answer set: explicit for CHOICE, the fixed pair for NOUL."""
        return self.options if self.options else NOUL_OPTIONS


@dataclass(frozen=True)
class Judgment:
    """A connector's typed answer.  ``label=None`` is a failed judgment."""

    label: str | None
    confidence: float
    backend: str
    latency_ms: float
    reason: str = "ok"

    @property
    def ok(self) -> bool:
        return self.label is not None

    @classmethod
    def failed(cls, *, backend: str, reason: str, latency_ms: float = 0.0) -> Judgment:
        return cls(
            label=None, confidence=0.0, backend=backend, latency_ms=latency_ms, reason=reason
        )


class JudgmentBackend(Protocol):
    """The seam every connector implements."""

    @property
    def name(self) -> str: ...

    async def judge(self, question: JudgmentQuestion) -> Judgment:
        """Answer *question*.  Must not raise — fail closed instead."""
        ...


def render_question_prompt(question: JudgmentQuestion) -> str:
    """Generic prompt rendering: instructions, state lines, answer hint.

    The instruction block is a stable prefix so provider-side prompt
    caching applies across calls — only the state varies.
    """
    options = question.resolved_options()
    lines = [question.instructions, ""]
    lines.extend(f"{key}: {value}" for key, value in question.state)
    lines.append(f"Answer with exactly one of: {', '.join(options)}.")
    lines.append("Answer:")
    return "\n".join(lines)


def parse_judgment(text: str, options: Sequence[str]) -> str | None:
    """Strictly extract an option label from a chat reply.

    Accepts a bare label with optional whitespace/punctuation, matched
    case-insensitively, and returns the option's canonical spelling.
    Anything else — prose, multiple labels, empty — returns ``None`` so
    the caller fails closed.
    """
    stripped = text.strip().strip(".:\"'`")
    for option in options:
        if stripped.casefold() == option.casefold():
            return option
    return None


Renderer = Callable[[JudgmentQuestion], str]


class WorkerChatJudgmentBackend:
    """Default connector: strict prompt-and-parse over the worker tier.

    Wraps the same ``async prompt -> text`` callable TD-703 used, so the
    seam's default path is byte-identical in behavior to the pre-seam
    classifier.  The chat reply carries no confidence signal, so a clean
    exact-label parse reports confidence 1.0; anything else fails closed.
    """

    def __init__(
        self,
        complete: Callable[[str], Awaitable[str]],
        *,
        name: str = "worker",
        renderer: Renderer = render_question_prompt,
    ) -> None:
        self._complete = complete
        self._name = name
        self._renderer = renderer

    @property
    def name(self) -> str:
        return self._name

    async def judge(self, question: JudgmentQuestion) -> Judgment:
        started = time.perf_counter()
        try:
            text = await self._complete(self._renderer(question))
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            log.warning(
                "judgment backend call failed; failing closed",
                extra={"extra_fields": {"backend": self._name, "error": str(exc)}},
            )
            return Judgment.failed(backend=self._name, reason="error", latency_ms=latency_ms)
        latency_ms = (time.perf_counter() - started) * 1000.0
        label = parse_judgment(text, question.resolved_options())
        if label is None:
            return Judgment.failed(
                backend=self._name, reason="parse_failure", latency_ms=latency_ms
            )
        return Judgment(
            label=label,
            confidence=1.0,
            backend=self._name,
            latency_ms=latency_ms,
        )


class ScriptedJudgmentBackend:
    """Test double: a queue of judgments (or one repeated), recording questions.

    An empty queue fails closed — the same shape a dead connector takes,
    so tests exercise the fail-closed path by omission.
    """

    def __init__(self, judgments: Sequence[Judgment] = (), *, name: str = "scripted") -> None:
        self._queue: list[Judgment] = list(judgments)
        self._name = name
        self.questions: list[JudgmentQuestion] = []

    @property
    def name(self) -> str:
        return self._name

    async def judge(self, question: JudgmentQuestion) -> Judgment:
        self.questions.append(question)
        if not self._queue:
            return Judgment.failed(backend=self._name, reason="empty_script")
        judgment = self._queue.pop(0)
        return Judgment(
            label=judgment.label,
            confidence=judgment.confidence,
            backend=self._name,
            latency_ms=judgment.latency_ms,
            reason=judgment.reason,
        )


def ideal_judgment(label: str, confidence: float = 0.95) -> Judgment:
    """A stand-in for a perfect typed model in evals — the ceiling, not a claim."""
    return Judgment(label=label, confidence=confidence, backend="ideal", latency_ms=0.0)
