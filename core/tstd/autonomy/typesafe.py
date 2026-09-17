"""TypeSafe (Jev) judgment connector (TD-708, optional).

The seam's second connector: ``JudgmentBackend`` over TypeSafe's System
One evaluation endpoint.  Selected only by explicit configuration
(``judgments.backend: typesafe``) with the key in the OS keychain —
never in config, logs, or the audit database (prime §2.2).  The worker
tier remains the default connector; nothing here is required.

Mapping (DECISIONS.md, 2026-09-17): a CHOICE question maps to a ``choice``
question whose criteria keys are the options; the answer carries the pick
and a distribution-derived confidence — the real signal the worker chat
connector cannot produce.  A NOUL answer is a probability, not a verdict:
the label is ``yes`` at p ≥ 0.5, and confidence is the distance from the
coin-flip point (``abs(p - 0.5) * 2``), per TypeSafe's own guidance that
0.5 means undecided, not "medium".

Every failure — transport, auth, malformed answer, missing key — fails
closed: a ``Judgment`` with ``label=None``, which callers map to their
safe default.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..logging import get_logger
from .judgment import Judgment, JudgmentKind, JudgmentQuestion

log = get_logger("tstd.judgment.typesafe")


class TypeSafeJudgmentBackend:
    """``JudgmentBackend`` over TypeSafe's ``/v1/systemone`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "jev-latest",
        timeout_seconds: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._http = client

    @property
    def name(self) -> str:
        return "typesafe"

    async def judge(self, question: JudgmentQuestion) -> Judgment:
        started = time.perf_counter()
        try:
            answers = await self._evaluate(question)
            judgment = self._map_answer(question, answers)
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            log.warning(
                "typesafe judgment failed; failing closed",
                extra={"extra_fields": {"error": str(exc)[:200]}},
            )
            return Judgment.failed(backend=self.name, reason="error", latency_ms=latency_ms)
        latency_ms = (time.perf_counter() - started) * 1000.0
        return Judgment(
            label=judgment[0],
            confidence=judgment[1],
            backend=self.name,
            latency_ms=latency_ms,
            reason="ok" if judgment[0] is not None else "parse_failure",
        )

    async def _evaluate(self, question: JudgmentQuestion) -> dict[str, Any]:
        qid = question.question_id or "q"
        body: dict[str, Any] = {
            "state": dict(question.state),
            "model": self._model,
            "questions": {qid: _question_body(question)},
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        url = f"{self._base_url}/v1/systemone"
        if self._http is not None:
            response = await self._http.post(url, json=body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                response = await http.post(url, json=body, headers=headers)
        response.raise_for_status()
        payload = response.json()
        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict) or qid not in answers:
            raise ValueError("response missing the question's answer")
        answer = answers[qid]
        if not isinstance(answer, dict):
            raise ValueError("answer is not an object")
        return answer

    @staticmethod
    def _map_answer(question: JudgmentQuestion, answer: dict[str, Any]) -> tuple[str | None, float]:
        if question.kind is JudgmentKind.CHOICE:
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in question.resolved_options():
                return None, 0.0
            confidence = answer.get("confidence")
            return choice, float(confidence) if isinstance(confidence, (int, float)) else 1.0
        probability = answer.get("noul")
        if not isinstance(probability, (int, float)):
            return None, 0.0
        p = max(0.0, min(1.0, float(probability)))
        return ("yes" if p >= 0.5 else "no"), abs(p - 0.5) * 2


def _question_body(question: JudgmentQuestion) -> dict[str, Any]:
    if question.kind is JudgmentKind.CHOICE:
        return {
            "type": "choice",
            "instructions": question.instructions,
            "criteria": {option: None for option in question.resolved_options()},
        }
    return {"type": "noul", "instructions": question.instructions}
