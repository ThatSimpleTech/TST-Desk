"""Offline accuracy of recorded classifier judgments (TD-708).

The fixture file holds classifier-signal cases and the replies a
connector already gave.  Nothing here opens a socket.  The runner
sends each case through the real seam (static table, then the
connector, then fail-toward-B) and reports accuracy and the share of
answers that landed on B.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .classifier import Boundary, DecisionClass, DecisionClassifier, DecisionRequest
from .judgment import Judgment, JudgmentQuestion
from .worker import AmbiguousClassifier

_OPTIONS = ("A", "B", "C")


@dataclass(frozen=True)
class ConnectorScore:
    """Accuracy and B-rate for one recorded connector."""

    connector: str
    correct: int
    total: int
    b_count: int

    @property
    def accuracy(self) -> str:
        return f"{self.correct}/{self.total}"

    @property
    def b_rate(self) -> str:
        return f"{self.b_count}/{self.total}"


def run_recorded_eval(path: Path) -> list[ConnectorScore]:
    """Score *path* and return one row per connector, in file order."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("judgment fixture must be an object")
    return asyncio.run(_evaluate(payload))


def format_report(scores: list[ConnectorScore]) -> str:
    """One header line and one line per connector.  Stable for the test."""
    lines = ["connector accuracy b_rate"]
    lines.extend(
        f"{score.connector} accuracy={score.accuracy} b_rate={score.b_rate}" for score in scores
    )
    return "\n".join(lines)


async def _evaluate(payload: dict[str, Any]) -> list[ConnectorScore]:
    cases = _cases(payload)
    threshold = payload.get("min_confidence", 0.6)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("min_confidence must be a number")
    connectors = payload.get("connectors")
    if not isinstance(connectors, list) or not connectors:
        raise ValueError("fixture needs a connectors list")
    scores: list[ConnectorScore] = []
    for raw in connectors:
        if not isinstance(raw, dict):
            raise ValueError("connector entry must be an object")
        scores.append(await _score_connector(cases, raw, float(threshold)))
    return scores


async def _score_connector(
    cases: list[dict[str, Any]], raw: dict[str, Any], min_confidence: float
) -> ConnectorScore:
    name = raw.get("name")
    kind = raw.get("kind")
    replies = raw.get("replies")
    if not isinstance(name, str) or not isinstance(kind, str) or not isinstance(replies, dict):
        raise ValueError("connector needs name, kind, and replies")
    workspace = Path("/tmp/tstd-judgment-eval")
    boundary = Boundary(workspace_root=workspace, allowed_hosts=frozenset())
    static = DecisionClassifier(boundary)
    backend: _ChatRecording | _TypedRecording
    if kind == "chat":
        backend = _ChatRecording(name, replies)
    elif kind == "typed":
        backend = _TypedRecording(name, replies)
    else:
        raise ValueError(f"unknown connector kind {kind!r}")
    classifier = AmbiguousClassifier(static, backend=backend, min_confidence=min_confidence)
    correct = 0
    b_count = 0
    for case in cases:
        request = _request(case)
        if static.classify(request).decision_class is not None:
            raise ValueError(f"case {case['id']} is not ambiguous; it never reaches the seam")
        expected = DecisionClass(str(case["expected"]))
        decision = await classifier.classify(request)
        if decision.decision_class is expected:
            correct += 1
        if decision.decision_class is DecisionClass.B:
            b_count += 1
    return ConnectorScore(connector=name, correct=correct, total=len(cases), b_count=b_count)


def _cases(payload: dict[str, Any]) -> list[dict[str, Any]]:
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("fixture needs cases")
    checked: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("case must be an object")
        if case.get("expected") not in _OPTIONS:
            raise ValueError(f"case {case.get('id')} expected must be A, B, or C")
        checked.append(case)
    return checked


def _request(case: dict[str, Any]) -> DecisionRequest:
    tool = case.get("tool_name")
    if not isinstance(tool, str) or not tool:
        raise ValueError(f"case {case.get('id')} needs a tool_name")
    provenance = case.get("provenance")
    return DecisionRequest(
        tool_name=tool,
        arguments={"case": str(case.get("id", tool))},
        hosts=frozenset(case.get("hosts") or ()),
        is_mutation=bool(case.get("mutation", False)),
        side_effect_class=str(case.get("side_effect_class", "auto")),
        provenance=provenance if isinstance(provenance, str) and provenance else None,
    )


class _ChatRecording:
    """Plays a recorded chat reply through the same strict label parse."""

    def __init__(self, name: str, replies: dict[str, Any]) -> None:
        self._name = name
        self._replies = replies

    @property
    def name(self) -> str:
        return self._name

    @property
    def remote_host(self) -> str | None:
        return None

    async def judge(self, question: JudgmentQuestion) -> Judgment:
        from .judgment import parse_judgment

        tool = dict(question.state).get("Tool", "")
        reply = self._replies.get(tool)
        if not isinstance(reply, str):
            raise ValueError(f"no chat recording for {tool}")
        if reply == "__timeout__":
            return Judgment.failed(backend=self._name, reason="timeout")
        if reply == "__error__":
            return Judgment.failed(backend=self._name, reason="error")
        label = parse_judgment(reply, question.resolved_options())
        if label is None:
            return Judgment.failed(backend=self._name, reason="parse_failure")
        return Judgment(label=label, confidence=1.0, backend=self._name, latency_ms=0.0)


class _TypedRecording:
    """Plays a recorded typed answer, confidence included."""

    def __init__(self, name: str, replies: dict[str, Any]) -> None:
        self._name = name
        self._replies = replies

    @property
    def name(self) -> str:
        return self._name

    @property
    def remote_host(self) -> str | None:
        return None

    async def judge(self, question: JudgmentQuestion) -> Judgment:
        tool = dict(question.state).get("Tool", "")
        reply = self._replies.get(tool)
        if not isinstance(reply, dict):
            raise ValueError(f"no typed recording for {tool}")
        label = reply.get("label")
        confidence = reply.get("confidence", 0.0)
        if label is not None and not isinstance(label, str):
            return Judgment.failed(backend=self._name, reason="parse_failure")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence = 0.0
        if label is None:
            return Judgment.failed(backend=self._name, reason="error")
        return Judgment(
            label=label,
            confidence=float(confidence),
            backend=self._name,
            latency_ms=0.0,
        )
