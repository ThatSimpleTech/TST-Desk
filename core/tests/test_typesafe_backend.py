"""TypeSafe judgment connector tests (TD-708, dev build).

The optional hosted connector against a mock transport — no network, no
spend.  The contract: typed answers with real confidence in, fail-closed
judgments out.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tstd.autonomy import JudgmentKind, JudgmentQuestion
from tstd.autonomy.typesafe import TypeSafeJudgmentBackend


def _backend(
    payload: dict[str, object], *, status: int = 200
) -> tuple[TypeSafeJudgmentBackend, list[dict[str, object]]]:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "body": json.loads(request.content.decode()),
                "auth": request.headers.get("authorization"),
                "url": str(request.url),
            }
        )
        return httpx.Response(status, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        TypeSafeJudgmentBackend(
            base_url="https://api.typesafe.ai",
            api_key="ts-test-key",
            model="jev-latest",
            client=client,
        ),
        seen,
    )


def _choice() -> JudgmentQuestion:
    return JudgmentQuestion(
        kind=JudgmentKind.CHOICE,
        instructions="Classify this tool call.",
        state=(("Tool", "fs_edit"),),
        options=("A", "B", "C"),
        question_id="classifier",
    )


def _noul() -> JudgmentQuestion:
    return JudgmentQuestion(
        kind=JudgmentKind.NOUL,
        instructions="Did the action land?",
        state=(("State after", "dialog open"),),
        question_id="cu-verify",
    )


class TestChoiceMapping:
    async def test_choice_maps_label_and_real_confidence(self) -> None:
        backend, seen = _backend(
            {
                "model": "jev-latest",
                "answers": {
                    "classifier": {
                        "type": "choice",
                        "choice": "B",
                        "probabilities": {"A": 0.1, "B": 0.8, "C": 0.1},
                        "confidence": 0.78,
                    }
                },
            }
        )
        judgment = await backend.judge(_choice())
        assert judgment.ok
        assert judgment.label == "B"
        assert judgment.confidence == 0.78
        assert judgment.backend == "typesafe"
        # The request is the API's shape: state object, model, criteria keys
        # are the options, and the key travels only as a header.
        request = seen[0]
        assert request["url"] == "https://api.typesafe.ai/v1/systemone"
        assert request["auth"] == "Bearer ts-test-key"
        body = request["body"]
        assert isinstance(body, dict)
        assert body["model"] == "jev-latest"
        assert body["state"] == {"Tool": "fs_edit"}
        questions = body["questions"]
        assert isinstance(questions, dict)
        criteria = questions["classifier"]["criteria"]  # type: ignore[index]
        assert set(criteria) == {"A", "B", "C"}

    async def test_out_of_set_choice_fails_closed(self) -> None:
        backend, _ = _backend(
            {"answers": {"classifier": {"type": "choice", "choice": "Z", "confidence": 0.9}}}
        )
        judgment = await backend.judge(_choice())
        assert not judgment.ok
        assert judgment.reason == "parse_failure"


class TestNoulMapping:
    async def test_probability_maps_to_label_and_confidence(self) -> None:
        backend, _ = _backend({"answers": {"cu-verify": {"type": "noul", "noul": 0.92}}})
        judgment = await backend.judge(_noul())
        assert judgment.label == "yes"
        assert judgment.confidence == pytest.approx(0.84)

    async def test_below_half_is_no(self) -> None:
        backend, _ = _backend({"answers": {"cu-verify": {"type": "noul", "noul": 0.3}}})
        judgment = await backend.judge(_noul())
        assert judgment.label == "no"
        assert judgment.confidence == pytest.approx(0.4)

    async def test_coin_flip_is_zero_confidence(self) -> None:
        backend, _ = _backend({"answers": {"cu-verify": {"type": "noul", "noul": 0.5}}})
        judgment = await backend.judge(_noul())
        assert judgment.confidence == 0.0


class TestFailClosed:
    async def test_http_error_fails_closed(self) -> None:
        backend, _ = _backend({"error": "nope"}, status=401)
        judgment = await backend.judge(_choice())
        assert not judgment.ok
        assert judgment.reason == "error"

    async def test_missing_answer_fails_closed(self) -> None:
        backend, _ = _backend({"answers": {}})
        judgment = await backend.judge(_choice())
        assert not judgment.ok

    async def test_malformed_answer_fails_closed(self) -> None:
        backend, _ = _backend({"answers": {"classifier": "B"}})
        judgment = await backend.judge(_choice())
        assert not judgment.ok


class TestBackendSelection:
    """loop.judgment_backend_for: the configured connector or a safe fallback."""

    async def test_worker_is_the_default(self) -> None:
        from tstd.config import JudgmentsConfig
        from tstd.loop import judgment_backend_for

        async def complete(prompt: str) -> str:
            return "B"

        backend = await judgment_backend_for(JudgmentsConfig(), complete)
        assert backend.name == "worker"

    async def test_typesafe_without_a_stored_key_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tstd.config import JudgmentsConfig
        from tstd.keychain import KeychainError
        from tstd.loop import judgment_backend_for

        async def no_key(_credential: str) -> str:
            raise KeychainError("not found")

        monkeypatch.setattr("tstd.loop.get_api_key", no_key)

        async def complete(prompt: str) -> str:
            return "B"

        backend = await judgment_backend_for(JudgmentsConfig(backend="typesafe"), complete)
        assert backend.name == "worker"

    async def test_typesafe_with_a_stored_key_selects_the_connector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tstd.config import JudgmentsConfig
        from tstd.loop import judgment_backend_for

        async def have_key(_credential: str) -> str:
            return "ts-stored"

        monkeypatch.setattr("tstd.loop.get_api_key", have_key)

        async def complete(prompt: str) -> str:
            return "B"

        backend = await judgment_backend_for(
            JudgmentsConfig(backend="typesafe", typesafe_base_url="https://api.typesafe.ai"),
            complete,
        )
        assert backend.name == "typesafe"

    async def test_typesafe_without_a_base_url_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tstd.config import JudgmentsConfig
        from tstd.loop import judgment_backend_for

        async def have_key(_credential: str) -> str:
            return "ts-stored"

        monkeypatch.setattr("tstd.loop.get_api_key", have_key)

        async def complete(prompt: str) -> str:
            return "B"

        backend = await judgment_backend_for(JudgmentsConfig(backend="typesafe"), complete)
        assert backend.name == "worker"
