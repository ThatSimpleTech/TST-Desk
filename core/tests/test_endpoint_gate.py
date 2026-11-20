"""How many model calls one server answers at a time (TD-5269).

One count per server, shared by every client in the daemon. These cases
are which URLs are one server, what config gives each, and that the count
really holds calls back, streams included, and lets go when a call ends,
fails or is cancelled.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.catalog_helpers import catalog_config_yaml
from tstd.config import ConfigError, load_config
from tstd.endpoint_gate import (
    EndpointGates,
    EndpointLimit,
    ModelConcurrencyConfig,
    endpoint_key,
)
from tstd.provider import ChatCompletionRequest, ChatMessage, ProviderClient, ProviderError

LOCAL = "http://127.0.0.1:8080/v1"


class TestWhichUrlsAreOneServer:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8080/v1",
            "http://localhost:8080",
            "http://127.0.0.1:8080/other/path/",
            "http://[::1]:8080/v1",
            "http://user:secret@127.0.0.1:8080/v1",
        ],
    )
    def test_every_spelling_of_this_computer_on_one_port(self, url: str) -> None:
        assert endpoint_key(url) == "http://this-computer:8080"
        assert "secret" not in (endpoint_key(url) or "")

    def test_another_port_scheme_or_host_is_another_server(self) -> None:
        keys = {
            endpoint_key("http://127.0.0.1:8080/v1"),
            endpoint_key("http://127.0.0.1:8081/v1"),
            endpoint_key("https://127.0.0.1:8080/v1"),
            endpoint_key("https://api.example.com/v1"),
            endpoint_key("https://API.example.com:443/v2"),
            endpoint_key("https://other.example.com/v1"),
        }
        assert len(keys) == 5  # the two api.example.com spellings are one

    @pytest.mark.parametrize("url", ["", "not a url", "ftp://host/x", "http://"])
    def test_what_names_no_server_has_no_gate(self, url: str) -> None:
        assert endpoint_key(url) is None
        assert EndpointGates(ModelConcurrencyConfig(loopback_default=1)).for_url(url) is None


class TestWhatConfigGives:
    def test_no_limit_unless_config_sets_one(self) -> None:
        gates = EndpointGates()
        assert gates.for_url(LOCAL) is None
        assert gates.for_url("https://api.example.com/v1") is None

    def test_a_default_for_this_computer_and_one_for_everything_else(self) -> None:
        gates = EndpointGates(ModelConcurrencyConfig(loopback_default=2, remote_default=8))
        assert gates.limit_for(LOCAL) == 2
        assert gates.limit_for("https://api.example.com/v1") == 8
        only_local = EndpointGates(ModelConcurrencyConfig(loopback_default=1))
        assert only_local.limit_for("https://api.example.com/v1") is None

    def test_a_server_named_by_url_has_its_own_number(self) -> None:
        config = ModelConcurrencyConfig(
            loopback_default=4,
            endpoints=[EndpointLimit(url="http://localhost:8080", max_concurrent=1)],
        )
        gates = EndpointGates(config)
        assert gates.limit_for(LOCAL) == 1
        assert gates.limit_for("http://127.0.0.1:11434/v1") == 4

    def test_every_client_for_one_server_shares_one_gate(self) -> None:
        gates = EndpointGates(ModelConcurrencyConfig(loopback_default=1))
        assert gates.for_url(LOCAL) is gates.for_url("http://localhost:8080/v2")
        assert gates.for_url(LOCAL) is not gates.for_url("http://127.0.0.1:11434/v1")

    def test_a_reload_keeps_a_gate_whose_number_did_not_change(self) -> None:
        gates = EndpointGates(ModelConcurrencyConfig(loopback_default=1))
        first = gates.for_url(LOCAL)
        gates.configure(ModelConcurrencyConfig(loopback_default=1, remote_default=3))
        assert gates.for_url(LOCAL) is first
        gates.configure(ModelConcurrencyConfig(loopback_default=2))
        assert gates.for_url(LOCAL) is not first
        gates.configure(ModelConcurrencyConfig())
        assert gates.for_url(LOCAL) is None

    @pytest.mark.parametrize("block", ["loopback_default: 0", "remote_default: 65", "limit: 1"])
    def test_a_bad_section_is_an_error_at_load(self, tmp_path: Path, block: str) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(catalog_config_yaml() + f"\nmodel_concurrency:\n  {block}\n")
        with pytest.raises(ConfigError, match="model_concurrency"):
            load_config(path)

    def test_the_shipped_config_limits_only_this_computers_servers(self, tmp_path: Path) -> None:
        # TD-5282: two for a model served here; a provider's server is not limited.
        path = tmp_path / "config.yaml"
        path.write_text(catalog_config_yaml(), encoding="utf-8")
        section = load_config(path).model_concurrency
        assert section.loopback_default == 2 and section.remote_default is None
        assert section.endpoints == []


REPLY = {
    "id": "x",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
STREAM = (
    b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
    b'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


class _Server:
    """A model server that counts how many calls it is answering at once."""

    def __init__(self, hold: float = 0.05) -> None:
        self.now = 0
        self.most = 0
        self.calls = 0
        self.hold = hold
        self.fail = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.now += 1
        self.most = max(self.most, self.now)
        try:
            await asyncio.sleep(self.hold)
            if self.fail:
                return httpx.Response(400, json={"error": {"message": "no"}})
            if b'"stream": true' in request.content or b'"stream":true' in request.content:
                return httpx.Response(
                    200, content=STREAM, headers={"content-type": "text/event-stream"}
                )
            return httpx.Response(200, json=REPLY)
        finally:
            self.now -= 1

    def client(self, base_url: str = LOCAL) -> ProviderClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return ProviderClient(base_url=base_url, api_key=None, client=http)


def _request(stream: bool = False) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="m", messages=[ChatMessage(role="user", content="hi")], stream=stream
    )


async def _drain(client: ProviderClient) -> list[Any]:
    return [chunk async for chunk in client.chat_completion_stream(_request(stream=True))]


class TestTheCountHolds:
    async def test_with_no_gate_calls_run_side_by_side(self) -> None:
        server = _Server()
        clients = [server.client() for _ in range(4)]
        await asyncio.gather(*(c.chat_completion(_request()) for c in clients))
        assert server.most == 4

    @pytest.mark.parametrize("limit", [1, 2])
    async def test_calls_from_every_client_for_one_server_share_the_limit(self, limit: int) -> None:
        server = _Server()
        gates = EndpointGates(ModelConcurrencyConfig(loopback_default=limit))
        clients = [server.client() for _ in range(5)]
        for client in clients:
            client.gate = gates.for_url(client.base_url)
        replies = await asyncio.gather(*(c.chat_completion(_request()) for c in clients))
        assert all(not isinstance(r, ProviderError) for r in replies)
        assert server.calls == 5
        assert server.most == limit

    async def test_a_stream_holds_its_slot_until_it_ends(self) -> None:
        server = _Server()
        gates = EndpointGates(ModelConcurrencyConfig(loopback_default=1))
        clients = [server.client() for _ in range(3)]
        for client in clients:
            client.gate = gates.for_url(client.base_url)
        streams = await asyncio.gather(*(_drain(c) for c in clients))
        assert all(streams) and server.calls == 3
        assert server.most == 1

    async def test_another_server_is_not_held_back(self) -> None:
        server = _Server()
        gates = EndpointGates(ModelConcurrencyConfig(loopback_default=1))
        here = server.client(LOCAL)
        there = server.client("http://127.0.0.1:11434/v1")
        here.gate, there.gate = gates.for_url(here.base_url), gates.for_url(there.base_url)
        await asyncio.gather(here.chat_completion(_request()), there.chat_completion(_request()))
        assert server.most == 2

    async def test_a_failed_call_and_a_cancelled_one_give_their_slot_back(self) -> None:
        server = _Server(hold=0.2)
        gates = EndpointGates(ModelConcurrencyConfig(loopback_default=1))
        gate = gates.for_url(LOCAL)
        assert gate is not None
        first, waiting, later = server.client(), server.client(), server.client()
        for client in (first, waiting, later):
            client.gate = gate

        running = asyncio.create_task(first.chat_completion(_request()))
        queued = asyncio.create_task(waiting.chat_completion(_request()))
        await asyncio.sleep(0.05)
        # One is being answered, one is waiting for the slot.
        assert server.now == 1 and gate.locked()
        queued.cancel()
        running.cancel()
        await asyncio.gather(running, queued, return_exceptions=True)
        assert not gate.locked()

        server.fail = True
        server.hold = 0.0
        failed = await later.chat_completion(_request())
        assert isinstance(failed, ProviderError)
        assert not gate.locked()


async def test_the_daemon_gives_clients_for_one_server_one_gate(tmp_path: Path) -> None:
    from tstd.daemon import Daemon

    text = catalog_config_yaml().replace(
        "\nmodel_concurrency:\n  loopback_default: 2\n",
        "\nmodel_concurrency:\n  loopback_default: 1\n",
        1,
    )
    assert "loopback_default: 1" in text
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    daemon = Daemon(data_dir=tmp_path)
    local = daemon.config.presets["local"]
    vllm = daemon.config.presets["vllm"]
    assert local.brain is not None and local.worker is not None and vllm.brain is not None
    brain = await daemon._build_client(local.brain)
    worker = await daemon._build_client(local.worker)
    other = await daemon._build_client(vllm.brain)
    assert brain.gate is not None and brain.gate is worker.gate
    # Another port on this computer is another server with a count of its own.
    assert other.gate is not None and other.gate is not brain.gate


async def test_a_stream_the_reader_abandons_gives_its_slot_back_at_once() -> None:
    """The reader stops after one chunk: the exchange is closed there and
    then, so the slot is free and the connection hung up, not left to the
    garbage collector (found by test_provider_timeouts after TD-5269)."""
    server = _Server(hold=0.0)
    gates = EndpointGates(ModelConcurrencyConfig(loopback_default=1))
    client = server.client()
    client.gate = gates.for_url(client.base_url)
    assert client.gate is not None
    stream = client.chat_completion_stream(_request(stream=True))
    async for _chunk in stream:
        assert client.gate.locked()
        break
    await stream.aclose()  # type: ignore[attr-defined]
    assert not client.gate.locked()
    # And the next call is not left waiting.
    again = await asyncio.wait_for(client.chat_completion(_request()), 2)
    assert not isinstance(again, ProviderError)


class TestNewInstallDefault:
    """TD-5282: a new install limits a local model to two calls; an old one is unchanged."""

    def test_the_shipped_config_limits_a_local_server_to_two(self) -> None:
        import yaml

        from tstd.config import default_config_yaml

        shipped = yaml.safe_load(default_config_yaml())["model_concurrency"]
        assert shipped == {"loopback_default": 2, "remote_default": None, "endpoints": []}
        limits = ModelConcurrencyConfig.model_validate(shipped)
        gates = EndpointGates(limits)
        local = gates.for_url("http://127.0.0.1:8080/v1")
        assert local is not None and local._value == 2
        # Any spelling of this computer, any port: each server has its own two.
        assert gates.for_url("http://localhost:8080/v1") is local
        other = gates.for_url("http://127.0.0.1:11434/v1")
        assert other is not None and other is not local and other._value == 2
        # A provider's server is not limited.
        assert gates.for_url("https://api.example.test/v1") is None

    def test_an_older_config_file_gets_no_limit(self, tmp_path: Path) -> None:
        """The section is not one the loader fills in from the shipped file."""
        from tests.catalog_helpers import catalog_config_yaml
        from tstd.config import FILLED_FROM_SHIPPED, load_config

        assert "model_concurrency" not in FILLED_FROM_SHIPPED
        text = catalog_config_yaml()
        start = text.index("\nmodel_concurrency:")
        end = text.index("\n\n", start + 1)
        older = text[:start] + text[end:]
        assert "model_concurrency" not in older.replace("# ", "")
        path = tmp_path / "config.yaml"
        path.write_text(older, encoding="utf-8")
        limits = load_config(path).model_concurrency
        assert limits.loopback_default is None and limits.remote_default is None
        assert EndpointGates(limits).for_url("http://127.0.0.1:8080/v1") is None
