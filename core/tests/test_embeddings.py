"""Embeddings client (TD-2202).

OpenAI POST /v1/embeddings, destination from config, no host in Python.
A missing or loopback-down endpoint falls back to heading-match and does
not fail the turn. Never Ollama /api/embed.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.test_loop import make_config, start_loop, wait_for_turn
from tstd.config import EmbeddingsConfig, ModelConfig, Preset, SearchConfig, TierConfig
from tstd.context import MEMORY_PLACEHOLDER
from tstd.context.embeddings import EmbeddingsClient, load_memory_for_turn
from tstd.memory_store import memory_dir
from tstd.mock import MockProvider, Script
from tstd.router import TierRouter
from tstd.session import Session


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _tier() -> TierConfig:
    return TierConfig(
        slug="t",
        base_url="http://127.0.0.1:11434/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=8192,
        max_output_tokens=256,
    )


def _model_config(embeddings: EmbeddingsConfig) -> ModelConfig:
    preset = Preset(brain=_tier(), worker=_tier(), validator=_tier())
    return ModelConfig(
        presets={"t": preset},
        active_preset="t",
        search=SearchConfig(),
        embeddings=embeddings,
    )


class TestClient:
    def test_empty_base_url_disables(self) -> None:
        client = EmbeddingsClient(base_url="", model="nomic-embed-text", timeout_seconds=1)
        assert client.enabled is False

    def test_empty_model_disables(self) -> None:
        client = EmbeddingsClient(base_url="http://127.0.0.1:8080/v1", model="", timeout_seconds=1)
        assert client.enabled is False

    async def test_disabled_returns_none_without_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("disabled client must not open a transport")

        monkeypatch.setattr(httpx.AsyncClient, "post", _forbidden)
        client = EmbeddingsClient.from_config(_model_config(EmbeddingsConfig()))
        assert await client.embed_or_none(["hello"]) is None

    async def test_down_loopback_returns_none(self) -> None:
        client = EmbeddingsClient(
            base_url="http://127.0.0.1:1/v1",
            model="nomic-embed-text",
            timeout_seconds=0.2,
        )
        assert await client.embed_or_none(["hello"]) is None

    async def test_openai_embeddings_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        class _FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"data": [{"embedding": [0.25, 0.5], "index": 0}]}

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> _FakeClient:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def post(self, url: str, json: dict[str, object]) -> _FakeResponse:
                seen.append(url)
                assert json == {"model": "nomic-embed-text", "input": ["task"]}
                assert url.endswith("/embeddings")
                assert "/api/embed" not in url
                return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = EmbeddingsClient(
            base_url="http://127.0.0.1:8080/v1",
            model="nomic-embed-text",
            timeout_seconds=1,
        )
        vectors = await client.embed_or_none(["task"])
        assert vectors == [[0.25, 0.5]]
        assert seen == ["http://127.0.0.1:8080/v1/embeddings"]

    def test_module_never_names_ollama_embed(self) -> None:
        source = Path(__file__).parents[1] / "tstd" / "context" / "embeddings.py"
        text = source.read_text(encoding="utf-8")
        assert "/api/embed" not in text
        assert "127.0.0.1" not in text
        assert "localhost" not in text


class TestFallback:
    async def test_down_sidecar_still_loads_heading_match(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        _write(mem / "MEMORY.md", "durable: we pin ruff\n")
        _write(mem / "auth.md", "# Auth\nrefresh tokens weekly\n")
        _write(mem / "billing.md", "# Payments\nnever log cards\n")
        client = EmbeddingsClient(
            base_url="http://127.0.0.1:1/v1",
            model="nomic-embed-text",
            timeout_seconds=0.2,
        )
        load = await load_memory_for_turn(ws, "the auth token expired", client)
        assert load.names == ("MEMORY.md", "auth.md")
        assert "never log cards" not in (load.block or "")

    async def test_disabled_is_heading_match(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(memory_dir(ws) / "MEMORY.md", "index\n")
        load = await load_memory_for_turn(ws, "anything", EmbeddingsClient("", "", 1))
        assert load.names == ("MEMORY.md",)


class TestTurnDoesNotFail:
    async def test_down_sidecar_turn_still_completes(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws / "AGENTS.md", "root steering\n")
        _write(memory_dir(ws) / "MEMORY.md", "durable: we pin ruff\n")
        _write(memory_dir(ws) / "auth.md", "# Auth\nrefresh tokens weekly\n")
        config = make_config()
        config.embeddings = EmbeddingsConfig(
            base_url="http://127.0.0.1:1/v1",
            model="nomic-embed-text",
            timeout_seconds=0.2,
        )
        session = Session(str(ws))
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="ok")})
        runner = await start_loop(session, TierRouter(), mock, config)
        await session.add_user_message("the auth token expired")
        await wait_for_turn(session, 1)
        prompt = mock.calls[0].messages[0].content or ""
        assert "durable: we pin ruff" in prompt
        assert "refresh tokens weekly" in prompt
        assert MEMORY_PLACEHOLDER not in prompt
        await runner.cancel()
