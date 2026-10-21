"""The "Add a model" walkthrough and the empty shipped catalog (TD-5172).

A new install ships no presets. The daemon must start, report a first run
(``has_model`` False), and fail a turn with the walkthrough rather than
crash. The walkthrough lists a server's models, writes one single-model
preset with a label and description, makes it active, and sends a test
message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from websockets.asyncio.client import connect

from tests.catalog_helpers import catalog_config_yaml
from tstd.config import ConfigError, default_config_yaml, load_config
from tstd.config_write import save_model_preset
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.model_setup import list_endpoint_models, parse_model_list, preset_name_for
from tstd.protocol import PROTOCOL_VERSION


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _add(path: Path, name: str = "my-model", **over: Any) -> None:
    kwargs: dict[str, Any] = {
        "label": "My model",
        "description": "General coding and chat",
        "base_url": "https://api.example.com/v1/",
        "slug": "vendor/model-a",
        "credential": "openrouter",
        "context_window": 128000,
        "max_output_tokens": 8192,
        "path": path,
    }
    kwargs.update(over)
    save_model_preset(name, **kwargs)


class TestSaveModelPreset:
    def test_writes_into_the_shipped_empty_catalog(self, tmp_path: Path) -> None:
        path = _write(tmp_path, default_config_yaml())
        _add(path)
        cfg = load_config(path)
        preset = cfg.presets["my-model"]
        assert preset.label == "My model"
        assert preset.description == "General coding and chat"
        assert preset.picker == "primary"
        slugs = {getattr(preset, t).slug for t in ("brain", "worker", "validator")}
        assert slugs == {"vendor/model-a"}
        assert preset.brain.base_url == "https://api.example.com/v1"
        assert cfg.active_preset == "my-model"  # the only one: the fallback picks it

    def test_comments_around_the_catalog_survive(self, tmp_path: Path) -> None:
        path = _write(tmp_path, default_config_yaml())
        _add(path)
        text = path.read_text(encoding="utf-8")
        assert "# Models (presets). None ship" in text
        assert "# Web search." in text
        assert text.index("  my-model:") < text.index("# Web search.")

    def test_appends_after_existing_presets(self, tmp_path: Path) -> None:
        path = _write(tmp_path, catalog_config_yaml())
        _add(path)
        cfg = load_config(path)
        assert {"tst-default", "vllm", "my-model"} <= set(cfg.presets)
        assert cfg.presets["vllm"].brain.base_url == "http://127.0.0.1:8000/v1"

    def test_strings_are_quoted_not_yaml(self, tmp_path: Path) -> None:
        path = _write(tmp_path, default_config_yaml())
        _add(path, label='Odd: "x"\nactive_preset: evil', description="a # b")
        cfg = load_config(path)
        assert cfg.presets["my-model"].label == 'Odd: "x"\nactive_preset: evil'
        assert cfg.presets["my-model"].description == "a # b"

    def test_off_box_needs_a_key(self, tmp_path: Path) -> None:
        path = _write(tmp_path, default_config_yaml())
        with pytest.raises(ConfigError, match="needs an API key"):
            _add(path, credential=None)

    def test_loopback_needs_none(self, tmp_path: Path) -> None:
        path = _write(tmp_path, default_config_yaml())
        _add(path, credential=None, base_url="http://127.0.0.1:8000/v1")
        assert load_config(path).requires_api_key() is False

    def test_taken_name_is_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path, default_config_yaml())
        _add(path)
        with pytest.raises(ConfigError, match="already exists"):
            _add(path)

    def test_name_from_label(self) -> None:
        assert preset_name_for("My Model", set()) == "my-model"
        assert preset_name_for("My Model", {"my-model"}) == "my-model-2"


class TestModelList:
    def test_parses_ids_windows_and_prices(self) -> None:
        rows = parse_model_list(
            {
                "data": [
                    {"id": "b-model", "max_model_len": 32768},
                    {
                        "id": "a-model",
                        "context_length": 200000,
                        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                    },
                    {"nope": 1},
                ]
            }
        )
        assert rows is not None
        assert [r.id for r in rows] == ["a-model", "b-model"]
        assert rows[0].context_window == 200000
        assert rows[0].input_price == pytest.approx(3.0)
        assert rows[0].output_price == pytest.approx(15.0)
        assert rows[1].context_window == 32768
        assert rows[1].input_price is None

    def test_not_a_list(self) -> None:
        assert parse_model_list({"hello": "world"}) is None

    async def test_sends_the_key_and_reports_a_refusal(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(401, json={"error": "no"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reply = await list_endpoint_models("https://api.example.com/v1/", "sk-x", client=client)
        assert str(seen[0].url) == "https://api.example.com/v1/models"
        assert seen[0].headers["Authorization"] == "Bearer sk-x"
        assert reply.models == []
        assert reply.error is not None and "refused the key" in reply.error
        assert "sk-x" not in reply.model_dump_json()

    async def test_lists_models(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "Authorization" not in request.headers
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reply = await list_endpoint_models("http://127.0.0.1:8000/v1", None, client=client)
        assert reply.error is None
        assert [m.id for m in reply.models] == ["local-model"]


# ── The daemon on an empty catalog ───────────────────────────────────────


async def _start(tmp: Path, provider: MockProvider) -> tuple[Daemon, asyncio.Task[Any], Any]:
    daemon = Daemon(data_dir=tmp, provider=provider)
    task = asyncio.create_task(daemon.run())
    for _ in range(100):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    ws = await connect(f"ws://127.0.0.1:{daemon.ws_server.port}")
    await ws.send(
        json.dumps({"type": "hello", "token": daemon.ws_server.token, "version": PROTOCOL_VERSION})
    )
    assert json.loads(await ws.recv())["type"] == "hello_ack"
    return daemon, task, ws


async def _stop(task: asyncio.Task[Any], ws: Any) -> None:
    await ws.close()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _ask(ws: Any, msg: dict[str, Any], want: str) -> dict[str, Any]:
    await ws.send(json.dumps(msg))
    for _ in range(40):
        event = dict(json.loads(await asyncio.wait_for(ws.recv(), 10)))
        if event["type"] in (want, "error"):
            return event
    raise AssertionError(f"no {want} reply")


@pytest.mark.shipped_config
class TestFirstRun:
    async def test_empty_catalog_is_a_first_run(self, tmp_path: Path) -> None:
        _daemon, task, ws = await _start(tmp_path, MockProvider(default=Script(kind="text")))
        try:
            state = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
            assert state["has_model"] is False
            assert state["presets"] == []
            assert state["active_preset"] == ""
            assert state["key_required"] is False
        finally:
            await _stop(task, ws)

    async def test_a_turn_with_no_model_fails_with_the_walkthrough(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _daemon, task, ws = await _start(
            tmp_path / "data", MockProvider(default=Script(kind="stream", content="hi"))
        )
        try:
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            sid = opened["session_id"]
            await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
            done = await _ask(
                ws,
                {"type": "user_message", "session_id": sid, "content": "hello"},
                "turn_complete",
            )
            assert done["type"] == "turn_complete"
            assert done.get("error_code") == "model_unresolved"
        finally:
            await _stop(task, ws)

    async def test_add_model_then_test_it(self, tmp_path: Path) -> None:
        provider = MockProvider(default=Script(kind="text", content="I am working."))
        _daemon, task, ws = await _start(tmp_path, provider)
        try:
            state = await _ask(
                ws,
                {
                    "type": "add_model",
                    "label": "Local box",
                    "description": "A model on this machine",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "slug": "local-model",
                },
                "setup_state",
            )
            assert state["type"] == "setup_state", state
            assert state["has_model"] is True
            assert state["active_preset"] == "local-box"
            assert state["preset_labels"] == {"local-box": "Local box"}
            assert state["preset_descriptions"] == {"local-box": "A model on this machine"}
            assert state["key_required"] is False
            saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
            assert saved["active_preset"] == "local-box"
            assert saved["presets"]["local-box"]["worker"]["slug"] == "local-model"

            tested = await _ask(ws, {"type": "test_model"}, "model_tested")
            assert tested["ok"] is True, tested
            assert tested["preset"] == "local-box"
            assert tested["reply"]
        finally:
            await _stop(task, ws)

    async def test_add_model_off_box_without_a_key_is_refused(self, tmp_path: Path) -> None:
        _daemon, task, ws = await _start(tmp_path, MockProvider(default=Script(kind="text")))
        try:
            reply = await _ask(
                ws,
                {
                    "type": "add_model",
                    "label": "Hosted",
                    "base_url": "https://api.example.com/v1",
                    "slug": "vendor/model",
                },
                "setup_state",
            )
            assert reply["type"] == "error"
            assert "needs an API key" in reply["message"]
        finally:
            await _stop(task, ws)
