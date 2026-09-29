"""Ambiguous-case decision classifier — worker-tier fallback (TD-703).

Cases the static rule table cannot decide are classified by a worker-tier
model call with a tight, cached prompt.  The result is cached per
(tool, argument-shape) within a session so repeat calls do not re-spend.

Safety contract (spec §12.2): a classifier failure defaults to **B**,
never to A — fail toward asking, not toward acting.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging import get_logger
from .classifier import (
    Classification,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
    relative_parts,
)
from .judgment import (
    Judgment,
    JudgmentBackend,
    JudgmentKind,
    JudgmentQuestion,
    WorkerChatJudgmentBackend,
)

log = get_logger("tstd.judgment.classifier")

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


def build_classifier_prompt(request: DecisionRequest, workspace_root: Path | None = None) -> str:
    """Build the tight classifier prompt for *request*.

    The instruction prefix is byte-stable; only the signal block varies,
    so provider prefix caching applies across calls.  Rendered from the
    seam question so the public helper and the default connector cannot
    drift apart.
    """
    return legacy_classifier_renderer(classifier_question(request, workspace_root))


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


def _signal_path(path: Path, workspace_root: Path | None) -> str:
    """Workspace-relative form of *path*, or ``outside`` when it is not.

    An absolute path must not leave the machine.  ``outside`` keeps the
    signal ("this is not in the workspace") without the location.
    """
    if workspace_root is None:
        return "outside"
    parts = relative_parts(path, workspace_root)
    if not parts:
        return "outside"
    return "/".join(parts)


def classifier_question(
    request: DecisionRequest, workspace_root: Path | None = None
) -> JudgmentQuestion:
    """The ambiguous-case classification as a seam question.

    State is the classifier signals only: tool name, workspace-relative
    paths, hosts, mutation intent, ``side_effect_class``, and provenance.
    Raw arguments stay off the request — they can carry absolute paths,
    file contents, or conversation text, and the cache key already
    covers argument shape.
    """
    reads = tuple(_signal_path(path, workspace_root) for path in request.reads)
    writes = tuple(_signal_path(path, workspace_root) for path in request.writes)
    paths = ",".join([*(f"read:{item}" for item in reads), *(f"write:{item}" for item in writes)])
    provenance = request.provenance if request.provenance else ""
    return JudgmentQuestion(
        kind=JudgmentKind.CHOICE,
        instructions=CLASSIFIER_INSTRUCTION,
        state=(
            ("Tool", request.tool_name),
            ("Paths", paths),
            ("Hosts", ",".join(sorted(request.hosts))),
            ("Mutation", "yes" if request.is_mutation else "no"),
            ("Side effect", request.side_effect_class),
            ("Provenance", provenance),
        ),
        options=_CLASSIFIER_OPTIONS,
        question_id="classifier",
    )


def classifier_signal_digest(state: tuple[tuple[str, str], ...]) -> str:
    """SHA-256 of the signal block.  The paths themselves are not the digest."""
    payload = json.dumps(list(state), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JudgmentAudit:
    """One classifier judgment, ready for the append-only audit row.

    ``model`` and ``invoked`` are not stored.  The loop uses them to bill
    a hosted connector without writing a second row for the worker tier,
    which already bills inside its completion.
    """

    connector: str
    payload_digest: str
    label: str | None
    confidence: float
    latency_ms: float
    cost: float
    cache_hit: bool
    decision_class: DecisionClass
    model: str = ""
    invoked: bool = False


@dataclass(frozen=True)
class _CachedJudgment:
    """What a session cache hit can replay without another connector call."""

    decision_class: DecisionClass
    label: str | None
    confidence: float
    connector: str
    model: str


def legacy_classifier_renderer(question: JudgmentQuestion) -> str:
    """Render a classifier question with the worker tier's ``Class:`` trailer.

    The trailer is what the strict parser was built against.  The state
    block is the question's, which is signals only, so this renderer and
    ``build_classifier_prompt`` cannot drift.
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
    wrapped in the default worker-chat connector, which renders the
    signal question with the ``Class:`` trailer.  Pass ``backend=`` to
    plug in a different connector.  A judgment below ``min_confidence``
    fails toward B.
    """

    def __init__(
        self,
        static: DecisionClassifier,
        call_worker: Callable[[str], Awaitable[str]] | None = None,
        *,
        backend: JudgmentBackend | None = None,
        min_confidence: float = 0.0,
        max_cache_entries: int = DEFAULT_CACHE_LIMIT,
        on_audit: Callable[[JudgmentAudit], None] | None = None,
    ) -> None:
        if backend is None:
            if call_worker is None:
                raise ValueError("AmbiguousClassifier needs a backend or call_worker")
            backend = WorkerChatJudgmentBackend(call_worker, renderer=legacy_classifier_renderer)
        self._static = static
        self._backend = backend
        self._min_confidence = min_confidence
        self._max_cache_entries = max_cache_entries
        self._on_audit = on_audit
        # Cache key: (tool name, canonical argument shape).  The key is
        # the arguments, not the signal block, so it is the same whichever
        # connector answered.
        self._cache: dict[tuple[str, str], _CachedJudgment] = {}

    @property
    def cache_size(self) -> int:
        """Number of cached worker classifications."""
        return len(self._cache)

    def _cache_key(self, request: DecisionRequest) -> tuple[str, str]:
        return (request.tool_name, canonical_arguments(request.arguments))

    async def classify(self, request: DecisionRequest) -> Classification:
        """Classify *request*, consulting the connector when ambiguous.

        Returns:
            A :class:`Classification`.  The static table's result is
            returned as-is when a rule fires; ambiguous cases go to the
            connector and default to **B** on any failure, low confidence,
            or a disabled remote connector.
        """
        static = self._static.classify(request)
        if static.decision_class is not None:
            return static

        question = classifier_question(request, self._static.boundary.workspace_root)
        digest = classifier_signal_digest(question.state)
        if self._remote_blocked():
            self._emit(
                self._record(
                    digest,
                    label=None,
                    confidence=0.0,
                    latency_ms=0.0,
                    cost=0.0,
                    cache_hit=False,
                    decision_class=DecisionClass.B,
                    invoked=False,
                )
            )
            return Classification(
                decision_class=DecisionClass.B,
                rule=None,
                reason="remote connector disabled by the network boundary",
            )

        key = self._cache_key(request)
        cached = self._cache.get(key)
        if cached is not None:
            self._emit(
                JudgmentAudit(
                    connector=cached.connector,
                    payload_digest=digest,
                    label=cached.label,
                    confidence=cached.confidence,
                    latency_ms=0.0,
                    cost=0.0,
                    cache_hit=True,
                    decision_class=cached.decision_class,
                    model=cached.model,
                    invoked=False,
                )
            )
            return Classification(
                decision_class=cached.decision_class,
                rule=None,
                reason=f"cached worker classification ({cached.decision_class.value})",
            )

        judgment = await self._backend.judge(question)
        decision_class = _final_class(judgment, self._min_confidence)
        model = _backend_model(self._backend)
        self._store(
            key,
            _CachedJudgment(
                decision_class=decision_class,
                label=judgment.label,
                confidence=judgment.confidence,
                connector=judgment.backend,
                model=model,
            ),
        )
        self._emit(
            self._record(
                digest,
                label=judgment.label,
                confidence=judgment.confidence,
                latency_ms=judgment.latency_ms,
                cost=judgment.cost,
                cache_hit=False,
                decision_class=decision_class,
                invoked=True,
                model=model,
                connector=judgment.backend,
            )
        )
        return Classification(
            decision_class=decision_class,
            rule=None,
            reason=f"worker classification ({decision_class.value})",
        )

    def _record(
        self,
        digest: str,
        *,
        label: str | None,
        confidence: float,
        latency_ms: float,
        cost: float,
        cache_hit: bool,
        decision_class: DecisionClass,
        invoked: bool,
        model: str = "",
        connector: str | None = None,
    ) -> JudgmentAudit:
        return JudgmentAudit(
            connector=self._backend.name if connector is None else connector,
            payload_digest=digest,
            label=label,
            confidence=confidence,
            latency_ms=latency_ms,
            cost=cost,
            cache_hit=cache_hit,
            decision_class=decision_class,
            model=model,
            invoked=invoked,
        )

    def _emit(self, record: JudgmentAudit) -> None:
        """Hand the row to the observer.  A broken observer cannot change the class."""
        if self._on_audit is None:
            return
        try:
            self._on_audit(record)
        except Exception as exc:
            log.warning(
                "judgment audit observer failed",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )

    def _remote_blocked(self) -> bool:
        """A hosted connector whose host the charter does not allow.

        Empty ``allowed_hosts`` is ``network: deny``.  The worker tier
        has no remote host, so a deny charter does not turn the user's
        own model off.
        """
        host = getattr(self._backend, "remote_host", None)
        if not isinstance(host, str) or not host.strip():
            return False
        allowed = {item.casefold() for item in self._static.boundary.allowed_hosts}
        return host.strip().casefold() not in allowed

    def _store(self, key: tuple[str, str], cached: _CachedJudgment) -> None:
        """Store a classification, evicting oldest entries past the bound."""
        if len(self._cache) >= self._max_cache_entries and key not in self._cache:
            # Evict the oldest inserted entry (dict preserves insertion order).
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = cached


def _final_class(judgment: Judgment, min_confidence: float) -> DecisionClass:
    """Map a judgment to a class.  Anything not a confident A/B/C is B."""
    label = judgment.label
    if label is None or judgment.confidence < min_confidence:
        return DecisionClass.B
    try:
        return DecisionClass(label)
    except ValueError:
        return DecisionClass.B


def _backend_model(backend: JudgmentBackend) -> str:
    model = getattr(backend, "model", "")
    return model if isinstance(model, str) else ""
