"""Values the Microsoft connector can show or store (TD-4853).

Access and refresh tokens are secrets. ``repr`` omits them so a log of
the object cannot leak the keychain blob. Failure text never includes
the authorization code, either token, or an OAuth error description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Delegated scopes for a public client that sends mail as the signed-in user.
SCOPES = "offline_access Mail.Send User.Read"

OFF_MESSAGE = (
    "Microsoft sign-in is off until config.yaml sets connectors.microsoft.client_id. "
    "Register a public client with no secret, allow a loopback redirect on 127.0.0.1, "
    "grant offline_access, Mail.Send, and User.Read, and paste the client id. "
    "Hosts are connectors.microsoft.login_host and connectors.microsoft.graph_host. "
    "Tenant defaults to common."
)

TIMEOUT_MESSAGE = "Sign-in timed out. Try again."
STATE_MESSAGE = "Sign-in could not be verified. Connect again."
DENIED_MESSAGE = "Microsoft sign-in was cancelled."
FAILED_MESSAGE = "Microsoft sign-in failed."
NOT_CONNECTED = "Microsoft is not connected. Sign in from Settings → Email."
REFRESH_FAILED = "Microsoft sign-in expired. Sign in again in Settings."
NO_MAILBOX = "Microsoft account has no mailbox address."

_SAFE_ERROR = re.compile(r"^[a-z0-9_]+$")
_PHASES = frozenset({"off", "idle", "pending", "connected", "denied", "error", "expired"})
_PUBLIC_LIMIT = 300


class MicrosoftError(Exception):
    """A Microsoft failure whose message is safe to show and to log."""

    def __init__(self, message: str, *, phase: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.phase = phase if phase in _PHASES else "error"


@dataclass
class MicrosoftTokens:
    """Access token plus a rotating refresh token. Neither is logged."""

    access_token: str
    refresh_token: str
    expires_at: float

    def __repr__(self) -> str:
        return (
            "MicrosoftTokens("
            f"expires_at={self.expires_at!r}, has_refresh={bool(self.refresh_token)})"
        )


@dataclass(frozen=True)
class Callback:
    """One loopback redirect. The code is a secret and is not logged."""

    code: str
    state: str
    error: str

    def __repr__(self) -> str:
        return f"Callback(has_code={bool(self.code)}, error={self.error!r})"


def printable_secret(value: str) -> bool:
    """True when *value* can sit in the keychain blob (printable ASCII, no NUL)."""
    if not value or "\n" in value or "\x00" in value:
        return False
    return all(0x20 <= ord(ch) <= 0x7E for ch in value)


def public_oauth_message(code: str) -> str:
    """Map an OAuth error code to a sentence. Never the description."""
    if code == "access_denied":
        return DENIED_MESSAGE
    if code in {"invalid_grant", "expired_token", "interaction_required"}:
        return REFRESH_FAILED
    if _SAFE_ERROR.fullmatch(code):
        return f"Microsoft sign-in failed ({code})."
    return FAILED_MESSAGE


def oauth_phase(code: str) -> str:
    """Phase for a terminal OAuth error."""
    if code == "access_denied":
        return "denied"
    if code in {"invalid_grant", "expired_token", "interaction_required"}:
        return "expired"
    return "error"


def scrub(text: str, secrets: tuple[str, ...] | list[str]) -> str:
    """Drop secrets an HTTP library may have echoed, then truncate."""
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _PUBLIC_LIMIT:
        cleaned = cleaned[:_PUBLIC_LIMIT]
    return cleaned or FAILED_MESSAGE
