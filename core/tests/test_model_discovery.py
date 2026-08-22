"""Resolving a local model from its endpoint (TD-1805).

The shipped ``local`` preset names an endpoint but no model tag, so the
model has to come from somewhere on first use.  These tests pin the five
things that decision is answerable for:

1. what "unset" means in the schema — and that the other spelling is an
   error rather than a second meaning;
2. that a slug in config always wins, with no request made at all;
3. that an ambiguous endpoint is refused rather than guessed at;
4. that every failure is a typed error naming the endpoint and the fix;
5. that a remote tier can never reach discovery, at all.

Every endpoint here is a **real loopback HTTP server** rather than a
transport stub.  The loop builds its own client, so a stub would only prove
what the test injected; a socket proves what actually left the process —
the path requested, and the absence of any ``Authorization`` header (§2.2).
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tests.test_diagnostics import _connect_and_handshake as connect_and_handshake
from tests.test_diagnostics import _start_daemon as start_daemon
from tests.test_diagnostics import _stop_daemon as stop_daemon
from tests.test_loop import mock_factory, wait_for_turn
from tstd.config import (
    ConfigError,
    ModelConfig,
    ModelDiscoveryError,
    Preset,
    TierConfig,
    TierName,
    cached_config,
    default_config_yaml,
    load_config,
)
from tstd.discovery import discover_model, resolve_tier_slugs
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import TierState, TurnComplete
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

# ── A real /v1/models endpoint on loopback ─────────────────────────────


class ModelsEndpoint:
    """An OpenAI-compatible ``/v1/models`` server on 127.0.0.1.

    Records every request it is asked for, so a test can assert both what
    was sent and — for the "config wins" case — that nothing was.
    """

    def __init__(self, models: list[str], status: int = 200, body: str | None = None) -> None:
        self.models = models
        self.status = status
        self.body = body
        self.paths: list[str] = []
        self.headers: list[dict[str, str]] = []
        self._server: asyncio.Server | None = None
        self._port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}/v1"

    @property
    def request_count(self) -> int:
        return len(self.paths)

    async def start(self, port: int = 0) -> None:
        """Listen on *port*, or any free port.  Passing a port back after a
        stop is how a test brings the same endpoint up again."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self._port = self._server.sockets[0].getsockname()[1]

    @property
    def port(self) -> int:
        return self._port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = (await reader.readline()).decode("latin-1").strip()
        headers: dict[str, str] = {}
        while True:
            raw = await reader.readline()
            if raw in (b"\r\n", b"\n", b""):
                break
            name, _, value = raw.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        self.paths.append(request_line.split(" ")[1] if " " in request_line else request_line)
        self.headers.append(headers)

        payload = self.body
        if payload is None:
            entries = [{"id": m, "object": "model", "owned_by": "library"} for m in self.models]
            payload = json.dumps({"object": "list", "data": entries})
        raw_body = payload.encode()
        writer.write(
            f"HTTP/1.1 {self.status} X\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(raw_body)}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + raw_body
        )
        await writer.drain()
        writer.close()


@pytest.fixture
async def endpoint() -> AsyncIterator[ModelsEndpoint]:
    """An endpoint serving exactly one model."""
    server = ModelsEndpoint(["only-model:1b"])
    await server.start()
    yield server
    await server.stop()


def closed_endpoint() -> str:
    """A loopback URL with nothing listening — a bound-then-released port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


# ── Config helpers ─────────────────────────────────────────────────────


def local_tier(base_url: str, slug: str | None = None) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url=base_url,
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=32_768,
        max_output_tokens=4_096,
    )


def local_config(base_url: str, slug: str | None = None) -> ModelConfig:
    """A three-tier local preset, all tiers on one endpoint."""
    return ModelConfig(
        presets={
            "local": Preset(
                brain=local_tier(base_url, slug),
                worker=local_tier(base_url, slug),
                validator=local_tier(base_url, slug),
            )
        },
        active_preset="local",
    )


def shipped_config() -> ModelConfig:
    """The config.yaml that ships in the package, not the user's copy."""
    parsed: Any = yaml.safe_load(default_config_yaml())
    return ModelConfig.model_validate(parsed)


# ── What "unset" means ─────────────────────────────────────────────────


class TestUnsetMeansNull:
    def test_omitted_slug_is_unset(self) -> None:
        assert local_tier("http://127.0.0.1:9/v1").slug is None

    def test_explicit_null_is_the_same_as_omitted(self) -> None:
        """A bare ``slug:`` in YAML parses to ``None``, so the two spellings
        must agree — otherwise trailing whitespace would change meaning."""
        omitted = TierConfig.model_validate(
            yaml.safe_load(
                "base_url: http://127.0.0.1:9/v1\ninput_price: 0\noutput_price: 0\n"
                "cache_read_price: 0\ncontext_window: 100\nmax_output_tokens: 10\n"
            )
        )
        explicit = TierConfig.model_validate(
            yaml.safe_load(
                "slug:\nbase_url: http://127.0.0.1:9/v1\ninput_price: 0\noutput_price: 0\n"
                "cache_read_price: 0\ncontext_window: 100\nmax_output_tokens: 10\n"
            )
        )
        assert omitted.slug is None
        assert explicit.slug is None
        assert omitted == explicit

    def test_empty_string_slug_is_a_validation_error(self) -> None:
        """Not a third spelling of unset: an empty string is a half-finished
        edit, and treating it as "discover for me" would hide the typo."""
        with pytest.raises(ValidationError) as excinfo:
            local_tier("http://127.0.0.1:9/v1", slug="")
        assert "slug" in str(excinfo.value)

    def test_require_slug_returns_a_set_slug(self) -> None:
        assert local_tier("http://127.0.0.1:9/v1", "m").require_slug() == "m"

    def test_require_slug_refuses_an_unresolved_tier(self) -> None:
        """Rather than sending ``"model": null`` and reading the reply."""
        with pytest.raises(ModelDiscoveryError):
            local_tier("http://127.0.0.1:9/v1").require_slug()


# ── Remote tiers never discover ────────────────────────────────────────


class TestRemoteStaysAConfigError:
    def test_remote_tier_without_a_slug_fails_validation(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            local_tier("https://openrouter.ai/api/v1")
        assert "openrouter.ai" in str(excinfo.value)

    def test_remote_tier_without_a_slug_is_a_config_error_at_load(self, tmp_path: Path) -> None:
        """Caught when the file loads, naming the offending key — discovery
        is never reached, so it can never quietly fix a remote tier."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "active_preset": "remote",
                    "presets": {
                        "remote": {
                            tier: {
                                "base_url": "https://openrouter.ai/api/v1",
                                "input_price": 1.0,
                                "output_price": 1.0,
                                "cache_read_price": 1.0,
                                "context_window": 1000,
                                "max_output_tokens": 100,
                            }
                            for tier in ("brain", "worker", "validator")
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError) as excinfo:
            load_config(config_path)
        message = str(excinfo.value)
        assert "brain" in message
        assert "slug is required" in message

    async def test_a_remote_preset_makes_no_request(self) -> None:
        """Every remote tier names a slug by construction, so resolution is
        a no-op for the shipped remote presets."""
        config = shipped_config()
        config.active_preset = "tst-default"
        await resolve_tier_slugs(config)  # no endpoint exists to reach
        assert config.tier("brain").slug == "moonshotai/kimi-k3"


# ── The shipped preset ─────────────────────────────────────────────────


class TestShippedPreset:
    @pytest.mark.parametrize("preset", ["local", "vllm"])
    @pytest.mark.parametrize("tier", ["brain", "worker", "validator"])
    def test_loopback_presets_name_no_model_tag(self, preset: str, tier: TierName) -> None:
        """AC-1 / TD-3901: a fresh install must not carry one developer's tag."""
        config = shipped_config()
        config.active_preset = preset
        assert config.tiers()[tier].slug is None

    def test_vllm_preset_names_the_documented_endpoint(self) -> None:
        """TD-3901: vLLM's OpenAI server default, not a model id."""
        config = shipped_config()
        config.active_preset = "vllm"
        urls = {t.base_url for t in config.tiers().values()}
        assert len(urls) == 1
        assert "127.0.0.1:8000" in next(iter(urls))

    @pytest.mark.parametrize("preset", ["tst-default", "budget"])
    @pytest.mark.parametrize("tier", ["brain", "worker", "validator"])
    def test_remote_presets_still_name_a_model(self, preset: str, tier: TierName) -> None:
        config = shipped_config()
        config.active_preset = preset
        assert config.tiers()[tier].slug


# ── Discovery ──────────────────────────────────────────────────────────


class TestDiscovery:
    async def test_one_model_resolves(self, endpoint: ModelsEndpoint) -> None:
        config = local_config(endpoint.base_url)
        await resolve_tier_slugs(config)
        assert config.tier("brain").slug == "only-model:1b"

    async def test_a_slug_in_config_wins_and_asks_nothing(self, endpoint: ModelsEndpoint) -> None:
        """AC-2: config is authoritative, and the proof is that the endpoint
        was never contacted — not merely that its answer was ignored."""
        config = local_config(endpoint.base_url, slug="pinned-by-hand")
        await resolve_tier_slugs(config)
        assert config.tier("brain").slug == "pinned-by-hand"
        assert endpoint.request_count == 0

    async def test_it_asks_for_the_models_route(self, endpoint: ModelsEndpoint) -> None:
        await resolve_tier_slugs(local_config(endpoint.base_url))
        assert endpoint.paths == ["/v1/models"]

    async def test_it_sends_no_credential(self, endpoint: ModelsEndpoint) -> None:
        """§2.2 on the wire: keyless is provable, not just intended."""
        await resolve_tier_slugs(local_config(endpoint.base_url))
        assert "authorization" not in endpoint.headers[0]

    async def test_three_tiers_on_one_endpoint_ask_once(self, endpoint: ModelsEndpoint) -> None:
        config = local_config(endpoint.base_url)
        await resolve_tier_slugs(config)
        assert endpoint.request_count == 1
        assert {t.slug for t in config.tiers().values()} == {"only-model:1b"}

    async def test_resolution_is_idempotent(self, endpoint: ModelsEndpoint) -> None:
        """Callable per turn without a round-trip per turn."""
        config = local_config(endpoint.base_url)
        await resolve_tier_slugs(config)
        await resolve_tier_slugs(config)
        await resolve_tier_slugs(config)
        assert endpoint.request_count == 1

    async def test_it_writes_nothing_to_disk(self, tmp_path: Path) -> None:
        """§2.2/§2.7: the resolved slug lives in memory for this process.
        The next process re-reads the endpoint, so swapping the model on the
        server does not leave a stale tag behind in config.yaml."""
        server = ModelsEndpoint(["only-model:1b"])
        await server.start()
        try:
            config_path = tmp_path / "config.yaml"
            config_path.write_text(
                default_config_yaml().replace("http://127.0.0.1:11434/v1", server.base_url),
                encoding="utf-8",
            )
            before = config_path.read_bytes()
            config = load_config(config_path)
            config.active_preset = "local"
            await resolve_tier_slugs(config)
            assert config.tier("brain").slug == "only-model:1b"
            assert config_path.read_bytes() == before
        finally:
            await server.stop()


class TestDiscoveryFailures:
    async def test_several_models_is_refused_by_name(self) -> None:
        """AC: choosing arbitrarily is a silent surprise.  ``/v1/models``
        lists embedding models beside chat models, so "first" could bind the
        agent to something that cannot answer a chat completion at all."""
        server = ModelsEndpoint(["chat-model:9b", "nomic-embed-text", "tiny:0.5b"])
        await server.start()
        try:
            config = local_config(server.base_url)
            with pytest.raises(ModelDiscoveryError) as excinfo:
                await resolve_tier_slugs(config)
            message = str(excinfo.value)
            assert server.base_url in message
            for served in ("chat-model:9b", "nomic-embed-text", "tiny:0.5b"):
                assert served in message
            assert "slug:" in message
            assert config.tier("brain").slug is None
        finally:
            await server.stop()

    async def test_zero_models_names_the_endpoint_and_the_fix(self) -> None:
        server = ModelsEndpoint([])
        await server.start()
        try:
            with pytest.raises(ModelDiscoveryError) as excinfo:
                await resolve_tier_slugs(local_config(server.base_url))
            assert excinfo.value.endpoint == server.base_url
            assert "serves no models" in str(excinfo.value)
            assert excinfo.value.fix
        finally:
            await server.stop()

    async def test_unreachable_endpoint_names_the_endpoint_and_the_fix(self) -> None:
        url = closed_endpoint()
        with pytest.raises(ModelDiscoveryError) as excinfo:
            await resolve_tier_slugs(local_config(url))
        assert excinfo.value.endpoint == url
        assert "model server" in excinfo.value.fix

    async def test_a_non_200_answer_is_not_a_model(self) -> None:
        server = ModelsEndpoint([], status=404, body='{"error": "nope"}')
        await server.start()
        try:
            with pytest.raises(ModelDiscoveryError) as excinfo:
                await discover_model(server.base_url)
            assert "404" in str(excinfo.value)
        finally:
            await server.stop()

    async def test_a_non_json_answer_is_not_a_model(self) -> None:
        server = ModelsEndpoint([], body="<html>who knows</html>")
        await server.start()
        try:
            with pytest.raises(ModelDiscoveryError):
                await discover_model(server.base_url)
        finally:
            await server.stop()

    async def test_the_fix_names_the_tier_that_needs_editing(self) -> None:
        server = ModelsEndpoint(["a", "b"])
        await server.start()
        try:
            with pytest.raises(ModelDiscoveryError) as excinfo:
                await discover_model(server.base_url, tier="worker")
            assert "worker" in excinfo.value.fix
        finally:
            await server.stop()

    async def test_the_fix_names_the_endpoint_not_the_served_tags(self) -> None:
        """TD-3901: ``.fix`` points at the URL. The message may list ids;
        the doctor-facing fix must not."""
        server = ModelsEndpoint(["chat-model:9b", "nomic-embed-text", "tiny:0.5b"])
        await server.start()
        try:
            with pytest.raises(ModelDiscoveryError) as excinfo:
                await resolve_tier_slugs(local_config(server.base_url))
            assert server.base_url in excinfo.value.fix
            for served in ("chat-model:9b", "nomic-embed-text", "tiny:0.5b"):
                assert served not in excinfo.value.fix
        finally:
            await server.stop()


# ── Through the agent loop ─────────────────────────────────────────────


class TestLoopResolvesOnFirstUse:
    async def test_the_turn_requests_the_discovered_model(
        self, endpoint: ModelsEndpoint, tmp_path: Path
    ) -> None:
        """End to end: config names no model, the endpoint does, and that is
        the model the provider is asked for."""
        session = Session(str(tmp_path))
        mock = MockProvider(default=Script(kind="stream", content="hi"))
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s, TierRouter(), mock_factory(mock), local_config(endpoint.base_url)
            ),
        )
        await runner.start()
        try:
            await session.add_user_message("hello")
            await wait_for_turn(session, 1)
            assert [c.model for c in mock.calls] == ["only-model:1b"]
        finally:
            await runner.cancel()

    async def test_the_title_bar_is_told_the_discovered_model(
        self, endpoint: ModelsEndpoint, tmp_path: Path
    ) -> None:
        session = Session(str(tmp_path))
        mock = MockProvider(default=Script(kind="stream", content="hi"))
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s, TierRouter(), mock_factory(mock), local_config(endpoint.base_url)
            ),
        )
        await runner.start()
        try:
            await session.add_user_message("hello")
            await wait_for_turn(session, 1)
            states = [e for e in session.event_log.all_events if isinstance(e, TierState)]
            assert states
            assert states[-1].model_slugs["brain"] == "only-model:1b"
        finally:
            await runner.cancel()

    async def test_no_discovery_when_config_names_the_model(
        self, endpoint: ModelsEndpoint, tmp_path: Path
    ) -> None:
        session = Session(str(tmp_path))
        mock = MockProvider(default=Script(kind="stream", content="hi"))
        config = local_config(endpoint.base_url, slug="pinned-by-hand")
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(s, TierRouter(), mock_factory(mock), config),
        )
        await runner.start()
        try:
            await session.add_user_message("hello")
            await wait_for_turn(session, 1)
            assert [c.model for c in mock.calls] == ["pinned-by-hand"]
            assert endpoint.request_count == 0
        finally:
            await runner.cancel()

    async def test_a_dead_endpoint_fails_the_turn_not_the_session(self, tmp_path: Path) -> None:
        """The rule TD-1008 set for a missing key: the conversation survives,
        the user starts their model server, and the next message goes
        through.  Failing session open instead would keep a workspace from
        opening at all."""
        server = ModelsEndpoint(["started-late:7b"])
        session = Session(str(tmp_path))
        mock = MockProvider(default=Script(kind="stream", content="hi"))
        # Bind, note the port, release it: the first turn finds nothing
        # listening; the same port answers once the server starts.
        await server.start()
        port, base_url = server.port, server.base_url
        await server.stop()

        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s, TierRouter(), mock_factory(mock), local_config(base_url)
            ),
        )
        await runner.start()
        try:
            await session.add_user_message("hello")
            first = await wait_for_turn(session, 1)
            assert first.failed
            assert first.error_code == "model_unresolved"
            assert not mock.calls

            await server.start(port)
            await session.add_user_message("hello again")
            second = await wait_for_turn(session, 2)
            assert not second.failed
            assert [c.model for c in mock.calls] == ["started-late:7b"]
        finally:
            await runner.cancel()
            await server.stop()

    async def test_a_failed_discovery_bills_nothing(self, tmp_path: Path) -> None:
        """No call was made, so the turn's meter must not move."""
        session = Session(str(tmp_path))
        mock = MockProvider(default=Script(kind="stream", content="hi"))
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s, TierRouter(), mock_factory(mock), local_config(closed_endpoint())
            ),
        )
        await runner.start()
        try:
            await session.add_user_message("hello")
            turn = await wait_for_turn(session, 1)
            assert isinstance(turn, TurnComplete)
            assert turn.failed
            assert turn.cost == 0.0
            assert turn.tokens == 0
        finally:
            await runner.cancel()


# ── What the user is told ──────────────────────────────────────────────


class TestDoctorNamesTheEndpoint:
    async def test_the_provider_row_carries_the_endpoint_and_the_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-3's user-facing surface: a dead local endpoint must read as a
        provider problem with an actionable fix, not as a bad API key and
        not as a generic provider error."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        cached_config.cache_clear()
        url = closed_endpoint()
        daemon, task = await start_daemon(tmp_path / "data")
        daemon.config = local_config(url)
        try:
            ws = await connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            await ws.send(json.dumps({"type": "run_diagnostics"}))
            report = dict(json.loads(await ws.recv()))
            await ws.close()
        finally:
            await stop_daemon(task)
            cached_config.cache_clear()

        provider_row = next(c for c in report["checks"] if c["name"] == "provider")
        assert provider_row["status"] == "fail"
        assert url in provider_row["detail"]
        assert provider_row["fix"]
        key_row = next(c for c in report["checks"] if c["name"] == "api_key")
        assert key_row["status"] == "skip"
