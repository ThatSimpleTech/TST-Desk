"""Tests for the connection handshake and local auth protocol."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError
from websockets.asyncio.client import connect

from tstd.protocol import (
    PROTOCOL_VERSION,
    HandshakeError,
    Hello,
    build_error,
    build_hello_ack,
    parse_hello,
    validate_token,
    validate_version,
)
from tstd.ws import WebSocketServer


class TestHello:
    def test_parse_valid(self) -> None:
        raw = json.dumps({"type": "hello", "token": "abc123", "version": 1})
        hello = parse_hello(raw)
        assert hello.token == "abc123"
        assert hello.version == 1

    def test_parse_invalid_json(self) -> None:
        with pytest.raises(HandshakeError, match="bad_request"):
            parse_hello("not json")

    def test_parse_wrong_type(self) -> None:
        raw = json.dumps({"type": "not_hello", "token": "abc", "version": 1})
        with pytest.raises(HandshakeError, match="unknown_message"):
            parse_hello(raw)

    def test_parse_missing_token(self) -> None:
        raw = json.dumps({"type": "hello", "version": 1})
        with pytest.raises(HandshakeError):
            parse_hello(raw)

    def test_parse_empty_token(self) -> None:
        raw = json.dumps({"type": "hello", "token": "", "version": 1})
        with pytest.raises(HandshakeError):
            parse_hello(raw)

    def test_parse_invalid_version(self) -> None:
        raw = json.dumps({"type": "hello", "token": "abc", "version": "one"})
        with pytest.raises(HandshakeError):
            parse_hello(raw)

    def test_parse_not_a_dict(self) -> None:
        with pytest.raises(HandshakeError, match="bad_request"):
            parse_hello('["hello"]')

    @pytest.mark.parametrize(
        "frame",
        [
            {"type": "hello", "token": "ab" * 32, "version": "one"},
            {"type": "hello", "token": "ab" * 32, "version": -1},
            {"type": "hello", "token": ["ab" * 32], "version": 1},
            {"type": "start_pairing", "token": "ab" * 32},
        ],
    )
    def test_a_malformed_hello_never_quotes_the_token(self, frame: dict[str, object]) -> None:
        with pytest.raises(HandshakeError) as caught:
            parse_hello(json.dumps(frame))
        assert caught.value.code == "bad_request"
        assert caught.value.message == "Malformed hello message"
        assert "ab" * 32 not in str(caught.value)

    def test_hello_model_validation(self) -> None:
        """Hello directly validates via Pydantic."""
        hello = Hello(token="abc", version=1)
        assert hello.type == "hello"
        # Version >= 0 is accepted by the model; validate_hello() checks range
        hello0 = Hello(token="abc", version=0)
        assert hello0.version == 0
        with pytest.raises(ValidationError):
            Hello(token="abc", version=-1)  # negative version fails

    def test_features_default_empty_and_parse(self) -> None:
        """TD-5015: features opt a client into viewer or bridge behaviour."""
        assert parse_hello(json.dumps({"type": "hello", "token": "t", "version": 1})).features == []
        raw = {"type": "hello", "token": "t", "version": 1, "features": ["machines", "later"]}
        assert parse_hello(json.dumps(raw)).features == ["machines", "later"]

    @pytest.mark.parametrize(
        "features",
        [["x" * 33], ["a"] * 9, "machines", [1]],
    )
    def test_bad_features_are_a_malformed_hello(self, features: object) -> None:
        raw = {"type": "hello", "token": "ab" * 32, "version": 1, "features": features}
        with pytest.raises(HandshakeError) as caught:
            parse_hello(json.dumps(raw))
        assert caught.value.code == "bad_request"
        assert "ab" * 32 not in str(caught.value)


class TestVersionValidation:
    def test_valid_version(self) -> None:
        validate_version(1)  # should not raise

    def test_too_old(self) -> None:
        with pytest.raises(HandshakeError, match="version_unsupported"):
            validate_version(0)

    def test_too_new(self) -> None:
        with pytest.raises(HandshakeError, match="version_unsupported"):
            validate_version(PROTOCOL_VERSION + 1)


class TestTokenValidation:
    def test_valid_token(self) -> None:
        validate_token("abc123", "abc123")  # should not raise

    def test_invalid_token(self) -> None:
        with pytest.raises(HandshakeError, match="auth_failed"):
            validate_token("wrong", "correct")


class TestMessageBuilders:
    def test_hello_ack(self) -> None:
        msg = json.loads(build_hello_ack())
        assert msg["type"] == "hello_ack"
        assert msg["version"] == PROTOCOL_VERSION
        assert "machine_id" not in msg and "desktop_name" not in msg

    def test_hello_ack_names_the_computer(self) -> None:
        """TD-5015: which computer this is, for "same computer" and "this is me"."""
        msg = json.loads(build_hello_ack(machine_id="mid_0123456789abcdef", desktop_name="ezer"))
        assert msg["machine_id"] == "mid_0123456789abcdef"
        assert msg["desktop_name"] == "ezer"

    def test_error(self) -> None:
        msg = json.loads(build_error("test_code", "test message"))
        assert msg["type"] == "error"
        assert msg["code"] == "test_code"
        assert msg["message"] == "test message"


class TestHandshakeIntegration:
    """End-to-end handshake tests against a running server."""

    @pytest.mark.asyncio
    async def test_successful_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            uri = f"ws://127.0.0.1:{server.port}"

            async with connect(uri) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "token": server.token,
                            "version": PROTOCOL_VERSION,
                        }
                    )
                )
                response = await ws.recv()
                msg = json.loads(response)
                assert msg["type"] == "hello_ack"
                assert msg["version"] == PROTOCOL_VERSION
                # TD-5015: the ack says which computer answered.
                assert re.fullmatch(r"mid_[0-9a-f]{16}", msg["machine_id"])
                assert msg["machine_id"] == server.machine_id
                assert isinstance(msg["desktop_name"], str) and msg["desktop_name"]

            await server.stop()

    @pytest.mark.asyncio
    async def test_machine_id_survives_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            first = server.machine_id
            await server.stop()
            again = WebSocketServer(Path(tmp))
            await again.start()
            assert first is not None and again.machine_id == first
            await again.stop()

    @pytest.mark.asyncio
    async def test_bad_token_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            uri = f"ws://127.0.0.1:{server.port}"

            async with connect(uri) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "token": "wrong-token",
                            "version": PROTOCOL_VERSION,
                        }
                    )
                )
                response = await ws.recv()
                msg = json.loads(response)
                assert msg["type"] == "error"
                assert msg["code"] == "auth_failed"

            await server.stop()

    @pytest.mark.asyncio
    async def test_missing_token_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            uri = f"ws://127.0.0.1:{server.port}"

            async with connect(uri) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "version": PROTOCOL_VERSION,
                        }
                    )
                )
                response = await ws.recv()
                msg = json.loads(response)
                assert msg["type"] == "error"

            await server.stop()

    @pytest.mark.asyncio
    async def test_wrong_type_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            uri = f"ws://127.0.0.1:{server.port}"

            async with connect(uri) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "not_hello",
                            "token": server.token,
                            "version": PROTOCOL_VERSION,
                        }
                    )
                )
                response = await ws.recv()
                msg = json.loads(response)
                assert msg["type"] == "error"
                assert msg["code"] == "unknown_message"

            await server.stop()

    @pytest.mark.asyncio
    async def test_version_mismatch_too_old(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            uri = f"ws://127.0.0.1:{server.port}"

            async with connect(uri) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "token": server.token,
                            "version": 0,
                        }
                    )
                )
                response = await ws.recv()
                msg = json.loads(response)
                assert msg["type"] == "error"
                assert msg["code"] == "version_unsupported"

            await server.stop()

    @pytest.mark.asyncio
    async def test_a_malformed_hello_logs_and_returns_no_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret = "cd" * 32
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp), ping_interval=0)
            await server.start()
            try:
                with caplog.at_level("DEBUG"):
                    async with connect(f"ws://127.0.0.1:{server.port}") as ws:
                        await ws.send(
                            json.dumps({"type": "hello", "token": secret, "version": "x"})
                        )
                        msg = json.loads(await ws.recv())
            finally:
                await server.stop()
        assert msg == {"type": "error", "code": "bad_request", "message": "Malformed hello message"}
        ours = [r for r in caplog.records if r.name.startswith("tstd")]
        assert "handshake rejected" in " ".join(r.getMessage() for r in ours)
        assert secret not in " ".join(f"{r.getMessage()} {r.__dict__}" for r in ours)

    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            uri = f"ws://127.0.0.1:{server.port}"

            async with connect(uri) as ws:
                await ws.send("not json")
                response = await ws.recv()
                msg = json.loads(response)
                assert msg["type"] == "error"
                assert msg["code"] == "bad_request"

            await server.stop()

    @pytest.mark.asyncio
    async def test_token_rotates_on_restart(self) -> None:
        """Token is different after daemon restart."""
        with tempfile.TemporaryDirectory() as tmp:
            server1 = WebSocketServer(Path(tmp))
            await server1.start()
            token1 = server1.token
            await server1.stop()

            server2 = WebSocketServer(Path(tmp))
            await server2.start()
            token2 = server2.token
            await server2.stop()

            assert token1 != token2

    @pytest.mark.asyncio
    async def test_handshake_timeout(self) -> None:
        """A connection that never sends hello receives a timeout error."""
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp), handshake_timeout=0.5)
            await server.start()
            uri = f"ws://127.0.0.1:{server.port}"

            async with connect(uri) as ws:
                response = await ws.recv()
                msg = json.loads(response)
                assert msg["type"] == "error"
                assert msg["code"] == "handshake_timeout"

            await server.stop()
