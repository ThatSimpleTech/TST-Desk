"""TD-708 seam criteria that the dev build left unchecked.

Signals only, fail-toward-B per connector, the network gate, the cache
key, and the rule that core modules do not name a vendor.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
    Judgment,
    ScriptedJudgmentBackend,
    WorkerChatJudgmentBackend,
    build_classifier_prompt,
    ideal_judgment,
)
from tstd.autonomy.connector import classifier_connector, judgment_backend_for
from tstd.autonomy.typesafe import TypeSafeJudgmentBackend
from tstd.autonomy.worker import (
    JudgmentAudit,
    classifier_question,
    classifier_signal_digest,
    legacy_classifier_renderer,
)
from tstd.config import JudgmentsConfig
from tstd.keychain import KeychainError

_HOST = "https://api.example.test"


def _ambiguous(q: int = 1) -> DecisionRequest:
    return DecisionRequest(tool_name="custom_tool", arguments={"q": q})


def _static(tmp_path: Path, allowed: frozenset[str] = frozenset()) -> DecisionClassifier:
    return DecisionClassifier(Boundary(workspace_root=tmp_path, allowed_hosts=allowed))


def _answer(choice: str, confidence: float) -> dict[str, object]:
    return {"answers": {"classifier": {"choice": choice, "confidence": confidence}}}


class TestSignalsOnly:
    async def test_judgment_request_is_classifier_signals_only(self, tmp_path: Path) -> None:
        inside = tmp_path / "notes" / "plan.md"
        inside.parent.mkdir(parents=True)
        body = "FILEBODY-q7q7q7 not for the model"
        conversation = "User said please paste the whole chat"
        inside.write_text(body, encoding="utf-8")
        request = DecisionRequest(
            tool_name="custom_tool",
            arguments={"body": body, "path": str(inside), "note": conversation},
            reads=(inside,),
            hosts=frozenset({"b.example.test", "a.example.test"}),
            is_mutation=False,
            side_effect_class="auto",
            provenance="local",
        )
        question = classifier_question(request, tmp_path)
        state = dict(question.state)
        assert list(state) == [
            "Tool",
            "Paths",
            "Hosts",
            "Mutation",
            "Side effect",
            "Provenance",
        ]
        assert state["Paths"] == "read:notes/plan.md"
        assert state["Hosts"] == "a.example.test,b.example.test"
        assert state["Mutation"] == "no"
        assert state["Provenance"] == "local"
        rendered = json.dumps(list(question.state))
        for banned in (body, conversation, str(inside), "Arguments"):
            assert banned not in rendered

        outside = classifier_question(
            DecisionRequest(tool_name="custom_tool", reads=(Path("/etc/passwd"), inside)),
            tmp_path,
        )
        outside_text = json.dumps(list(outside.state))
        assert dict(outside.state)["Paths"] == "read:outside,read:notes/plan.md"
        assert "/etc/passwd" not in outside_text
        assert str(inside) not in outside_text

        rooted = classifier_question(
            DecisionRequest(tool_name="custom_tool", reads=(inside,)), None
        )
        assert dict(rooted.state)["Paths"] == "read:outside"
        assert str(inside) not in json.dumps(list(rooted.state))

        seen: list[dict[str, object]] = []

        def handler(http_request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(http_request.content.decode()))
            return httpx.Response(200, json=_answer("B", 0.8))

        allowed = frozenset({"a.example.test", "b.example.test", "api.example.test"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TypeSafeJudgmentBackend(base_url=_HOST, api_key="ts-test-key", client=client)
            await AmbiguousClassifier(_static(tmp_path, allowed), backend=backend).classify(request)
        assert len(seen) == 1
        posted = seen[0]["state"]
        assert posted == dict(question.state)
        raw = json.dumps(seen[0])
        for banned in (body, conversation, str(inside)):
            assert banned not in raw

    async def test_static_short_circuit_writes_no_audit_row(self, tmp_path: Path) -> None:
        rows: list[JudgmentAudit] = []
        backend = ScriptedJudgmentBackend([ideal_judgment("A")])
        classifier = AmbiguousClassifier(_static(tmp_path), backend=backend, on_audit=rows.append)
        outside = DecisionRequest(tool_name="fs_read", reads=(Path("/etc/passwd"),))
        decision = await classifier.classify(outside)
        assert decision.decision_class is DecisionClass.C
        assert backend.questions == []
        assert rows == []


class TestFailTowardB:
    async def test_worker_timeout_classifies_as_b(self, tmp_path: Path) -> None:
        async def boom(_prompt: str) -> str:
            raise TimeoutError()

        backend = WorkerChatJudgmentBackend(boom, renderer=legacy_classifier_renderer)
        judgment = await backend.judge(classifier_question(_ambiguous(), tmp_path))
        assert judgment.reason == "timeout"
        assert judgment.label is None
        rows: list[JudgmentAudit] = []
        classifier = AmbiguousClassifier(_static(tmp_path), backend=backend, on_audit=rows.append)
        decision = await classifier.classify(_ambiguous())
        assert decision.decision_class is DecisionClass.B
        assert rows[0].label is None
        assert rows[0].decision_class is DecisionClass.B
        assert rows[0].invoked is True

    @pytest.mark.parametrize(
        ("status", "payload", "timed_out", "expected"),
        [
            (401, {"error": "no"}, False, DecisionClass.B),
            (200, _answer("Z", 0.9), False, DecisionClass.B),
            (200, _answer("A", 0.2), False, DecisionClass.B),
            (200, {"answers": {"classifier": "A"}}, False, DecisionClass.B),
            (200, _answer("A", 0.91), False, DecisionClass.A),
            (200, _answer("A", 0.91), True, DecisionClass.B),
        ],
    )
    async def test_typesafe_classifier_fails_toward_b(
        self,
        tmp_path: Path,
        status: int,
        payload: dict[str, object],
        timed_out: bool,
        expected: DecisionClass,
    ) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if timed_out:
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(status, json=payload)

        allowed = frozenset({"api.example.test"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TypeSafeJudgmentBackend(base_url=_HOST, api_key="ts-test-key", client=client)
            classifier = AmbiguousClassifier(
                _static(tmp_path, allowed), backend=backend, min_confidence=0.6
            )
            decision = await classifier.classify(_ambiguous())
        assert calls == 1
        assert decision.decision_class is expected

    async def test_low_confidence_caches_the_final_class(self, tmp_path: Path) -> None:
        backend = ScriptedJudgmentBackend(
            [Judgment(label="A", confidence=0.2, backend="typed", latency_ms=3.0, cost=0.01)],
            name="typed",
        )
        rows: list[JudgmentAudit] = []
        classifier = AmbiguousClassifier(
            _static(tmp_path),
            backend=backend,
            min_confidence=0.6,
            on_audit=rows.append,
        )
        first = await classifier.classify(_ambiguous())
        second = await classifier.classify(_ambiguous())
        assert first.decision_class is DecisionClass.B
        assert second.decision_class is DecisionClass.B
        assert len(backend.questions) == 1
        assert "cached" in second.reason
        assert rows[0].label == "A"
        assert rows[0].decision_class is DecisionClass.B
        assert rows[0].cost == 0.01
        assert rows[0].invoked is True
        assert rows[1].cache_hit is True
        assert rows[1].invoked is False
        assert rows[1].cost == 0.0
        assert rows[1].label == "A"
        assert rows[1].decision_class is DecisionClass.B


class TestNetworkGate:
    async def test_unlisted_remote_host_classifies_b_without_a_call(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=_answer("A", 0.9))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TypeSafeJudgmentBackend(base_url=_HOST, api_key="ts-test-key", client=client)
            rows: list[JudgmentAudit] = []
            classifier = AmbiguousClassifier(
                _static(tmp_path), backend=backend, on_audit=rows.append
            )
            first = await classifier.classify(_ambiguous())
            second = await classifier.classify(_ambiguous())
        assert calls == []
        assert first.decision_class is DecisionClass.B
        assert second.decision_class is DecisionClass.B
        assert "network boundary" in first.reason
        assert "network boundary" in second.reason
        assert "cached" not in second.reason
        assert [row.cache_hit for row in rows] == [False, False]
        assert all(row.invoked is False and row.label is None for row in rows)

    async def test_allowed_remote_host_is_called_once_and_then_cached(self, tmp_path: Path) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=_answer("A", 0.9))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TypeSafeJudgmentBackend(base_url=_HOST, api_key="ts-test-key", client=client)
            rows: list[JudgmentAudit] = []
            classifier = AmbiguousClassifier(
                _static(tmp_path, frozenset({"API.Example.Test"})),
                backend=backend,
                on_audit=rows.append,
            )
            first = await classifier.classify(_ambiguous())
            second = await classifier.classify(_ambiguous())
        assert calls == 1
        assert first.decision_class is DecisionClass.A
        assert second.decision_class is DecisionClass.A
        assert "cached" in second.reason
        assert rows[0].cache_hit is False and rows[0].invoked is True
        assert rows[1].cache_hit is True and rows[1].invoked is False
        assert rows[1].cost == 0.0

    async def test_worker_connector_still_runs_when_network_is_denied(self, tmp_path: Path) -> None:
        seen: list[str] = []

        async def spy(prompt: str) -> str:
            seen.append(prompt)
            return "C"

        classifier = AmbiguousClassifier(_static(tmp_path), call_worker=spy)
        decision = await classifier.classify(_ambiguous())
        assert decision.decision_class is DecisionClass.C
        assert seen == [build_classifier_prompt(_ambiguous(), tmp_path)]
        assert seen[0].endswith("Class:")


class TestCacheAndConnector:
    async def test_cache_key_is_tool_and_canonical_arguments(self, tmp_path: Path) -> None:
        # The signal block ignores arguments, so a cache on the prompt
        # would have treated these as one call.  The key is the arguments.
        backend = ScriptedJudgmentBackend(
            [ideal_judgment("C"), ideal_judgment("A"), ideal_judgment("B")],
            name="typed",
        )
        classifier = AmbiguousClassifier(_static(tmp_path), backend=backend)
        same = DecisionRequest(tool_name="custom_tool", arguments={"a": 1, "b": 2})
        flipped = DecisionRequest(tool_name="custom_tool", arguments={"b": 2, "a": 1})
        other = DecisionRequest(tool_name="custom_tool", arguments={"a": 9})
        await classifier.classify(same)
        await classifier.classify(flipped)
        await classifier.classify(other)
        assert len(backend.questions) == 2

    async def test_audit_observer_failure_does_not_change_the_class(self, tmp_path: Path) -> None:
        def boom(_record: JudgmentAudit) -> None:
            raise RuntimeError("disk full")

        classifier = AmbiguousClassifier(
            _static(tmp_path),
            backend=ScriptedJudgmentBackend([ideal_judgment("A")]),
            on_audit=boom,
        )
        decision = await classifier.classify(_ambiguous())
        assert decision.decision_class is DecisionClass.A

    async def test_classifier_connector_keeps_the_legacy_prompt(self, tmp_path: Path) -> None:
        seen: list[str] = []

        async def spy(prompt: str) -> str:
            seen.append(prompt)
            return "B"

        request = _ambiguous()
        backend = await classifier_connector(JudgmentsConfig(), spy)
        await backend.judge(classifier_question(request, tmp_path))
        assert seen == [build_classifier_prompt(request, tmp_path)]
        assert seen[0].endswith("Class:")
        digest = classifier_signal_digest(classifier_question(request, tmp_path).state)
        assert len(digest) == 64

    async def test_classifier_connector_uses_hosted_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def have_key(_credential: str) -> str:
            return "ts-stored"

        monkeypatch.setattr("tstd.autonomy.connector.get_api_key", have_key)

        async def spy(_prompt: str) -> str:
            raise AssertionError("worker completion should not run")

        backend = await classifier_connector(
            JudgmentsConfig(backend="typesafe", typesafe_base_url=_HOST),
            spy,
        )
        assert backend.name == "typesafe"
        assert getattr(backend, "remote_host", None) == "api.example.test"

    async def test_classifier_connector_falls_back_without_a_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        async def no_key(_credential: str) -> str:
            raise KeychainError("missing")

        monkeypatch.setattr("tstd.autonomy.connector.get_api_key", no_key)
        seen: list[str] = []

        async def spy(prompt: str) -> str:
            seen.append(prompt)
            return "A"

        cfg = JudgmentsConfig(backend="typesafe", typesafe_base_url=_HOST)
        question = classifier_question(_ambiguous(), tmp_path)
        selected = await judgment_backend_for(cfg, spy)
        await selected.judge(question)
        assert selected.name == "worker"
        assert seen[0].endswith("Answer:")
        seen.clear()
        fallback = await classifier_connector(cfg, spy)
        await fallback.judge(question)
        assert fallback.name == "worker"
        assert seen[0].endswith("Class:")


def test_core_modules_do_not_name_a_vendor() -> None:
    root = Path(__file__).resolve().parents[1] / "tstd"
    named = (
        "loop.py",
        "autonomy/judgment.py",
        "autonomy/worker.py",
        "autonomy/classifier.py",
    )
    pattern = re.compile(r"typesafe|type-safe|\bjev\b", re.IGNORECASE)
    for rel in named:
        text = (root / rel).read_text(encoding="utf-8")
        assert pattern.search(text) is None, rel
    for path in root.rglob("*.py"):
        assert "typesafe.ai" not in path.read_text(encoding="utf-8").casefold()
