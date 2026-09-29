"""TD-708 audit rows and classifier-cost accounting."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import httpx
import pytest

from tests.test_cost import WORKER, _usage
from tests.test_worker_classifier import make_config
from tstd.audit import AuditStore
from tstd.audit import DecisionClass as AuditClass
from tstd.audit_writer import AuditWriter, forward_classifier_judgment
from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
)
from tstd.autonomy.judgment import WorkerChatJudgmentBackend, note_judgment_completion_cost
from tstd.autonomy.typesafe import TypeSafeJudgmentBackend
from tstd.autonomy.worker import JudgmentAudit, classifier_question
from tstd.config import JudgmentsConfig, default_config_yaml
from tstd.config_write import save_judgments
from tstd.cost import CostTracker
from tstd.session import Session

_HOST = "https://api.example.test"


def _database_bytes(store: AuditStore) -> bytes:
    """Checkpoint and read the file. Path I/O stays out of async tests."""
    store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = store.db_path.read_bytes()
    wal = Path(str(store.db_path) + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
    return blob


def _record(**overrides: object) -> JudgmentAudit:
    base: dict[str, object] = {
        "connector": "typesafe",
        "payload_digest": "ab" * 32,
        "label": "A",
        "confidence": 0.9,
        "latency_ms": 4.0,
        "cost": 0.004,
        "cache_hit": False,
        "decision_class": DecisionClass.A,
        "model": "alias",
        "invoked": True,
    }
    base.update(overrides)
    return JudgmentAudit(**base)  # type: ignore[arg-type]


class _Sink:
    def __init__(self) -> None:
        self.rows: list[JudgmentAudit] = []

    def record_model_call(self, session_id: str, record: object, is_classifier: bool) -> None:
        return None

    def record_judgment(self, session_id: str, record: JudgmentAudit) -> None:
        self.rows.append(record)


class TestClassifierCost:
    def test_hosted_judgment_cost_stays_on_the_classifier_line(self) -> None:
        tracker = CostTracker(make_config())
        tracker.begin_turn()
        sink = _Sink()
        worker = _record(connector="worker", cost=0.2, model="test-worker")
        forward_classifier_judgment(worker, session_id="s", writer=sink, tracker=tracker)
        missed = _record(cache_hit=True, invoked=False, cost=0.5)
        forward_classifier_judgment(missed, session_id="s", writer=sink, tracker=tracker)
        hosted = _record()
        forward_classifier_judgment(hosted, session_id="s", writer=sink, tracker=tracker)
        noted = tracker.record_classifier("worker", _usage(200, 0, 5), WORKER)
        assert tracker.classifier_cost() == pytest.approx(0.004 + noted)
        assert tracker.session_cost() == 0.0
        assert tracker.turn_cost() == 0.0
        event = tracker.emit_cost_update("s")
        assert event.classifier_cost == pytest.approx(tracker.classifier_cost())
        assert event.session_cost == 0.0
        assert len(sink.rows) == 3
        assert tracker.record_classifier_amount("typesafe", "alias", -1.0) == 0.0

    async def test_worker_completion_is_not_billed_twice(self) -> None:
        tracker = CostTracker(make_config())

        async def complete(_prompt: str) -> str:
            cost = tracker.record_classifier("worker", _usage(100, 0, 4), WORKER)
            note_judgment_completion_cost(cost)
            return "B"

        backend = WorkerChatJudgmentBackend(complete)
        judgment = await backend.judge(
            classifier_question(DecisionRequest(tool_name="custom_tool"))
        )
        assert judgment.cost == pytest.approx(tracker.classifier_cost())
        forward_classifier_judgment(
            JudgmentAudit(
                connector=judgment.backend,
                payload_digest="ab" * 32,
                label=judgment.label,
                confidence=judgment.confidence,
                latency_ms=judgment.latency_ms,
                cost=judgment.cost,
                cache_hit=False,
                decision_class=DecisionClass.B,
                invoked=True,
            ),
            session_id="s",
            writer=None,
            tracker=tracker,
        )
        assert tracker.classifier_call_count() == 1
        assert tracker.session_cost() == 0.0


class TestJudgmentWriter:
    async def test_writer_persists_a_judgment_row(self, tmp_path: Path) -> None:
        store = AuditStore(tmp_path / "audit.db")
        writer = AuditWriter(store)
        writer.start()
        session = Session(str(tmp_path))
        writer.attach_session(session)
        digest = "ef" * 32
        writer.record_judgment(
            session.id,
            JudgmentAudit(
                connector="typed",
                payload_digest=digest,
                label="C",
                confidence=0.5,
                latency_ms=4.0,
                cost=0.0,
                cache_hit=False,
                decision_class=DecisionClass.C,
                invoked=True,
            ),
        )
        await writer._queue.join()
        row = store._conn.execute(
            "SELECT session_id, connector, payload_digest, label, decision_class, cache_hit"
            " FROM judgments"
        ).fetchone()
        assert row == (session.id, "typed", digest, "C", "C", 0)
        await writer.close()


class TestCredentialHygiene:
    async def test_connector_credential_stays_out_of_config_logs_and_audit(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        canary = "sk-" + "m" * 24
        cfg = JudgmentsConfig(backend="typesafe", typesafe_base_url=_HOST)
        assert canary not in cfg.model_dump_json()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(default_config_yaml(), encoding="utf-8")
        save_judgments(cfg, config_path)
        assert canary not in config_path.read_text(encoding="utf-8")

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("authorization", ""))
            return httpx.Response(401, json={"error": "no"})

        caplog.set_level(logging.DEBUG, logger="tstd")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TypeSafeJudgmentBackend(base_url=_HOST, api_key=canary, client=client)
            failed = await backend.judge(
                classifier_question(DecisionRequest(tool_name="custom_tool"), tmp_path)
            )
            assert failed.label is None
            assert failed.reason == "error"
            store = AuditStore(tmp_path / "audit.db")
            try:
                store.append_session("s1", str(tmp_path), started_at=1.0)
                rows: list[JudgmentAudit] = []
                classifier = AmbiguousClassifier(
                    DecisionClassifier(
                        Boundary(
                            workspace_root=tmp_path,
                            allowed_hosts=frozenset({"api.example.test"}),
                        )
                    ),
                    backend=backend,
                    on_audit=rows.append,
                )
                decision = await classifier.classify(
                    DecisionRequest(tool_name="custom_tool", arguments={"q": 1})
                )
                assert decision.decision_class is DecisionClass.B
                assert len(rows) == 1
                record = rows[0]
                store.append_judgment(
                    "s1",
                    record.connector,
                    record.payload_digest,
                    record.label,
                    record.confidence,
                    record.latency_ms,
                    record.cost,
                    record.cache_hit,
                    cast(AuditClass, record.decision_class.value),
                )
                assert canary.encode() not in _database_bytes(store)
            finally:
                store.close()
        assert seen == [f"Bearer {canary}", f"Bearer {canary}"]
        assert canary not in caplog.text
        for rec in caplog.records:
            assert canary not in rec.getMessage()
