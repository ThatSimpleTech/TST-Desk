"""Connection handshake and local auth protocol.

Every client must present its auth token in a `hello` message before
the server processes any other messages. The handshake also negotiates
the protocol version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Current protocol version
PROTOCOL_VERSION = 1

# Minimum supported version
MIN_PROTOCOL_VERSION = 1


class HandshakeError(Exception):
    """Raised when a handshake fails. Carries a typed error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass
class HelloMessage:
    """The client's opening handshake message."""

    token: str
    version: int

    @classmethod
    def parse(cls, raw: str) -> HelloMessage:
        """Parse a raw JSON string into a HelloMessage.

        Raises:
            HandshakeError: If the message is malformed.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HandshakeError(
                "bad_request",
                f"Invalid JSON in handshake: {e}",
            ) from e

        if not isinstance(data, dict):
            raise HandshakeError("bad_request", "Handshake must be a JSON object")

        if data.get("type") != "hello":
            raise HandshakeError(
                "bad_request",
                f"Expected type='hello', got {data.get('type', 'none')!r}",
            )

        token = data.get("token", "")
        if not isinstance(token, str) or not token:
            raise HandshakeError("auth_missing", "Missing or empty 'token' field")

        version = data.get("version", 0)
        if not isinstance(version, int):
            raise HandshakeError(
                "bad_request",
                f"Invalid 'version' {version!r}: must be an integer",
            )
        if version < 0:
            raise HandshakeError(
                "bad_request",
                f"Invalid 'version' {version!r}: cannot be negative",
            )

        return cls(token=token, version=version)


def validate_version(version: int) -> None:
    """Check that the client's protocol version is compatible.

    Raises:
        HandshakeError: If the version is incompatible.
    """
    if version < MIN_PROTOCOL_VERSION:
        raise HandshakeError(
            "version_unsupported",
            f"Protocol version {version} is too old. "
            f"Minimum supported: {MIN_PROTOCOL_VERSION}. "
            f"Current: {PROTOCOL_VERSION}. "
            f"Please upgrade your client.",
        )
    if version > PROTOCOL_VERSION:
        raise HandshakeError(
            "version_unsupported",
            f"Protocol version {version} is too new. "
            f"Server supports: {PROTOCOL_VERSION}. "
            f"Please upgrade the daemon.",
        )


def validate_token(provided: str, expected: str) -> None:
    """Check that the client's token matches the server's.

    Raises:
        HandshakeError: If the token is invalid.
    """
    if provided != expected:
        raise HandshakeError(
            "auth_failed",
            "Invalid auth token. Check the token in the port file and try again.",
        )


def build_hello_ack() -> str:
    """Build the server's handshake acknowledgement."""
    return json.dumps({"type": "hello_ack", "version": PROTOCOL_VERSION})


def build_error(code: str, message: str) -> str:
    """Build a typed error message."""
    return json.dumps({"type": "error", "code": code, "message": message})
