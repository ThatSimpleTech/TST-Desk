"""Whether each model on the picker can run right now (TD-5240).

Three things can be known without sending a key or asking a provider: the
key is in the keychain, a server on this computer answers, the Grok CLI is
there. The picker shows the first one that is not true.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.test_add_model import _ask, _start, _stop
from tests.test_preset_engine import _text as _catalog_with_grok
from tstd.config import ModelConfig, load_config
from tstd.local_servers import loopback_answers
from tstd.mock import MockProvider, Script
from tstd.peer_policy import MACHINE_PEER_VERBS
from tstd.preset_ready import check_presets, loopback_urls

OLLAMA = "http://127.0.0.1:11434/v1"
VLLM = "http://127.0.0.1:8000/v1"


def _config(tmp_path: Path) -> ModelConfig:
    path = tmp_path / "config.yaml"
    path.write_text(_catalog_with_grok(), encoding="utf-8")
    return load_config(path)


async def _rows(
    config: ModelConfig,
    *,
    stored: set[str] | None = None,
    down: set[str] | None = None,
    grok: bool = True,
) -> dict[str, tuple[str, str]]:
    asked_keys: list[str] = []
    asked_urls: list[str] = []

    async def key_stored(credential: str) -> bool:
        asked_keys.append(credential)
        return credential in (stored if stored is not None else {"openrouter"})

    async def answers(urls: list[str]) -> Mapping[str, bool]:
        asked_urls.extend(urls)
        return {url: url not in (down or set()) for url in urls}

    rows = await check_presets(config, key_stored=key_stored, answers=answers, grok_found=grok)
    assert [row.preset for row in rows] == list(config.presets)
    # Only servers on this computer are ever asked.
    assert all(url.startswith("http://127.0.0.1") for url in asked_urls)
    return {row.preset: (row.status, row.detail) for row in rows}


class TestTheChecks:
    async def test_everything_in_place_is_ready(self, tmp_path: Path) -> None:
        rows = await _rows(_config(tmp_path))
        assert {status for status, _detail in rows.values()} == {"ready"}
        assert all(detail == "" for _status, detail in rows.values())

    async def test_a_key_that_is_not_stored(self, tmp_path: Path) -> None:
        rows = await _rows(_config(tmp_path), stored=set())
        assert rows["tst-default"] == ("no_key", "No key is stored for OpenRouter.")
        assert rows["budget"][0] == "no_key"
        # A model on this computer needs no key, and Grok brings its own.
        assert rows["local"][0] == "ready" and rows["grok"][0] == "ready"

    async def test_a_server_on_this_computer_that_does_not_answer(self, tmp_path: Path) -> None:
        rows = await _rows(_config(tmp_path), down={OLLAMA})
        assert rows["local"] == (
            "server_down",
            "Nothing is answering at 127.0.0.1:11434. Start the model server.",
        )
        assert rows["vllm"][0] == "ready"
        assert rows["tst-default"][0] == "ready"

    async def test_a_grok_entry_without_the_grok_cli(self, tmp_path: Path) -> None:
        rows = await _rows(_config(tmp_path), grok=False)
        assert rows["grok"] == ("no_grok", "The Grok CLI was not found on this computer.")
        assert rows["local"][0] == "ready"

    async def test_the_first_problem_a_person_would_fix_is_the_one_shown(
        self, tmp_path: Path
    ) -> None:
        # A local role bound to a key that is missing, on a server that is down.
        text = (
            _catalog_with_grok()
            .replace(
                f"      base_url: {OLLAMA}\n",
                f"      base_url: {OLLAMA}\n      credential: house\n",
            )
            .replace("\ncredentials:\n", "\ncredentials:\n  house:\n    name: House key\n", 1)
        )
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        rows = await _rows(load_config(path), down={OLLAMA})
        assert rows["local"] == ("no_key", "No key is stored for House key.")

    async def test_a_providers_server_is_never_asked(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        assert loopback_urls(config) == [OLLAMA, VLLM]

    async def test_with_the_grok_engine_as_the_setting_every_entry_is_a_grok_one(
        self, tmp_path: Path
    ) -> None:
        text = _catalog_with_grok(engine_kind="grok")
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        rows = await _rows(load_config(path), stored=set(), down={OLLAMA}, grok=False)
        assert {status for status, _detail in rows.values()} == {"no_grok"}


class TestTheProbe:
    async def _ask(self, handler: Any, urls: list[str]) -> tuple[dict[str, bool], list[str]]:
        seen: list[str] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            assert "authorization" not in {k.lower() for k in request.headers}
            return handler(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
            return await loopback_answers(urls, client=client), seen

    async def test_any_reply_means_a_server_is_there(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404 if "11434" in str(request.url) else 200, json={})

        found, seen = await self._ask(handler, [OLLAMA, VLLM + "/"])
        assert found == {OLLAMA: True, VLLM: True}
        assert sorted(seen) == sorted([f"{VLLM}/models", f"{OLLAMA}/models"])

    async def test_a_refused_connection_or_a_timeout_means_it_is_not(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "11434" in str(request.url):
                raise httpx.ConnectError("refused", request=request)
            raise httpx.ReadTimeout("slow", request=request)

        found, _seen = await self._ask(handler, [OLLAMA, VLLM])
        assert found == {OLLAMA: False, VLLM: False}

    async def test_a_url_off_this_computer_is_not_asked_and_carries_no_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        found, seen = await self._ask(handler, ["https://api.example.com/v1", OLLAMA])
        assert seen == [f"{OLLAMA}/models"]
        assert found == {"https://api.example.com/v1": True, OLLAMA: True}

    async def test_nothing_local_asks_nothing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request expected")

        found, seen = await self._ask(handler, ["https://api.example.com/v1"])
        assert seen == [] and found == {"https://api.example.com/v1": True}


class TestOverTheWire:
    async def test_check_presets_answers_with_a_row_per_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[list[str]] = []

        async def answers(urls: list[str]) -> dict[str, bool]:
            asked.append(list(urls))
            return {url: "8000" in url for url in urls}

        monkeypatch.setattr("tstd.daemon.loopback_answers", answers)
        monkeypatch.setattr("tstd.daemon._probe_grok", lambda _configured: (None, False))
        data = tmp_path / "data"
        data.mkdir()
        (data / "config.yaml").write_text(_catalog_with_grok(), encoding="utf-8")
        _daemon, task, ws = await _start(data, MockProvider(default=Script(kind="text")))
        try:
            reply = await _ask(ws, {"type": "check_presets"}, "preset_readiness")
            assert reply["type"] == "preset_readiness"
            rows = {row["preset"]: row for row in reply["rows"]}
            assert rows["local"]["status"] == "server_down"
            assert "127.0.0.1:11434" in rows["local"]["detail"]
            assert rows["vllm"]["status"] == "ready"
            assert rows["grok"]["status"] == "no_grok"
            # The suite stores no key, so the hosted entries say so.
            assert rows["tst-default"]["status"] == "no_key"
            assert asked == [[OLLAMA, VLLM]]
        finally:
            await _stop(task, ws)


def test_another_computers_hub_may_not_send_it() -> None:
    assert "check_presets" not in MACHINE_PEER_VERBS
