"""Ambiguous-case decision classifier — worker-tier fallback (TD-703).

Cases the static rule table cannot decide are classified by a worker-tier
model call with a tight, cached prompt.  The result is cached per
(tool, argument-shape) within a session so repeat calls do not re-spend.

Safety contract (spec §12.2): a classifier failure defaults to **B**,
never to A — fail toward asking, not toward acting.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from .classifier import (
    Classification,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
)
from .judgment import (
    JudgmentBackend,
    JudgmentKind,
    JudgmentQuestion,
    WorkerChatJudgmentBackend,
)

# The instruction block is a stable prefix so provider-side prompt caching
# applies across classification calls — only the tool call varies.
CLASSIFIER_INSTRUCTION = (
    "Classify the following tool call into exactly one of A, B, or C.\n"
    "A = reversible, inside the workspace, no external contract.\n"
    "B = reversible but costly to unwind.\n"
    "C = irreversible, outside the workspace, or over a declared cap.\n"
    "Reply with exactly one letter."
)

# Cache bound: a session performs a bounded number of distinct tool shapes.
DEFAULT_CACHE_LIMIT = 256


def canonical_arguments(arguments: dict[str, Any]) -> str:
    """A deterministic serialization of *arguments* for cache keys.

    Sorted keys and compact separators so equivalent dicts hash equal;
    non-JSON values (e.g. Path) fall back to their string form.
    """
    try:
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return json.dumps(
            {k: str(v) for k, v in arguments.items()},
            sort_keys=True,
            separators=(",", ":"),
        )


def build_classifier_prompt(request: DecisionRequest) -> str:
    """Build the tight classifier prompt for *request*.

    The instruction prefix is byte-stable; only the tool call varies, so
    provider prefix caching applies across calls.  TD-708: rendered from
    the seam question so the public helper and the default connector can
    never drift apart.
    """
    return _legacy_renderer(classifier_question(request))


def parse_decision(text: str) -> DecisionClass | None:
    """Extract the decision class from a worker response.

    Accepts a bare letter with optional whitespace/punctuation
    (``A``, `` B ``).  Anything else — prose, multiple letters, or an
    empty response — returns ``None`` so the caller fails toward B.
    """
    stripped = text.strip().strip(".:\"'`")
    upper = stripped.upper()
    if upper in {"A", "B", "C"}:
        return DecisionClass(upper)
    return None


# ── TD-708: the classifier speaks through the judgment seam ────────────

_CLASSIFIER_OPTIONS: tuple[str, ...] = ("A", "B", "C")


def classifier_question(request: DecisionRequest) -> JudgmentQuestion:
    """The ambiguous-case classification as a seam question.

    State is the classifier signals only — tool name and canonical
    arguments — never file contents or conversation text.
    """
    return JudgmentQuestion(
        kind=JudgmentKind.CHOICE,
        instructions=CLASSIFIER_INSTRUCTION,
        state=(
            ("Tool", request.tool_name),
            ("Arguments", canonical_arguments(request.arguments)),
        ),
        options=_CLASSIFIER_OPTIONS,
        question_id="classifier",
    )


def _legacy_renderer(question: JudgmentQuestion) -> str:
    """Reproduce the TD-703 prompt byte-for-byte from the question.

    The default connector keeps the pre-seam prompt wording (``Class:``
    trailer included) so formalizing the seam does not change what the
    worker model is asked.
    """
    lines = [question.instructions, ""]
    lines.extend(f"{key}: {value}" for key, value in question.state)
    lines.append("Class:")
    return "\n".join(lines)


class AmbiguousClassifier:
    """Static rule table with a worker-tier fallback for ambiguous cases.

    Usage::

        classifier = AmbiguousClassifier(
            static=DecisionClassifier(Boundary(workspace_root=ws)),
            call_worker=worker_fn,  # async: prompt -> model text
        )
        decision = await classifier.classify(request)

    ``classify`` is async because the fallback makes a model call; the
    static table is consulted first and short-circuits without any call.

    TD-708: the fallback is a ``JudgmentBackend``.  ``call_worker`` is
    wrapped in the default worker-chat connector (legacy prompt renderer,
    so the bytes are unchanged); pass ``backend=`` to plug in a different
    connector.  A judgment below ``min_confidence`` fails toward B.
    """

    def __init__(
        self,
        static: DecisionClassifier,
        call_worker: Callable[[str], Awaitable[str]] | None = None,
        *,
        backend: JudgmentBackend | None = None,
        min_confidence: float = 0.0,
        max_cache_entries: int = DEFAULT_CACHE_LIMIT,
    ) -> None:
        if backend is None:
            if call_worker is None:
                raise ValueError("AmbiguousClassifier needs a backend or call_worker")
            backend = WorkerChatJudgmentBackend(call_worker, renderer=_legacy_renderer)
        self._static = static
        self._backend = backend
        self._min_confidence = min_confidence
        self._max_cache_entries = max_cache_entries
        # Cache key: (tool name, canonical argument shape) -> class.
        self._cache: dict[tuple[str, str], DecisionClass] = {}

    @property
    def cache_size(self) -> int:
        """Number of cached worker classifications."""
        return len(self._cache)

    def _cache_key(self, request: DecisionRequest) -> tuple[str, str]:
        return (request.tool_name, canonical_arguments(request.arguments))

    async def classify(self, request: DecisionRequest) -> Classification:
        """Classify *request*, consulting the worker tier when ambiguous.

        Returns:
            A :class:`Classification`.  The static table's result is
            returned as-is when a rule fires; ambiguous cases go to the
            worker tier and default to **B** on any failure or unparseable
            response.
        """
        static = self._static.classify(request)
        if static.decision_class is not None:
            return static

        key = self._cache_key(request)
        cached = self._cache.get(key)
        if cached is not None:
            return Classification(
                decision_class=cached,
                rule=None,
                reason=f"cached worker classification ({cached.value})",
            )

        decision_class = await self._backend_classify(request)
        self._store(key, decision_class)
        return Classification(
            decision_class=decision_class,
            rule=None,
            reason=f"worker classification ({decision_class.value})",
        )

    async def _backend_classify(self, request: DecisionRequest) -> DecisionClass:
        """Run the seam judgment; failure or low confidence defaults to B."""
        judgment = await self._backend.judge(classifier_question(request))
        label = judgment.label
        if label is None or judgment.confidence < self._min_confidence:
            return DecisionClass.B
        try:
            return DecisionClass(label)
        except ValueError:
            # A connector answered outside the option set — fail toward B.
            return DecisionClass.B

    def _store(self, key: tuple[str, str], decision_class: DecisionClass) -> None:
        """Store a classification, evicting oldest entries past the bound."""
        if len(self._cache) >= self._max_cache_entries and key not in self._cache:
            # Evict the oldest inserted entry (dict preserves insertion order).
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = decision_class
