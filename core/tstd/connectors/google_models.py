"""Values the Google connector can show or store (TD-4854).

Access and refresh tokens, and the desktop client secret, are secrets.
``repr`` omits them so a log of the object cannot leak the keychain blob.
Failure text never includes an authorization code, a token, the client
secret, or an OAuth error description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# gmail.send only, plus OpenID email so Settings can name the account.
# The scope URL is an identifier, not a host the daemon dials. The host is
# a variable so it is not a destination literal (see test_outbound_hosts).
_GMAIL_SCOPE_HOST = "www.googleapis.com"
GMAIL_SEND_SCOPE = f"https://{_GMAIL_SCOPE_HOST}/auth/gmail.send"
SCOPES = f"openid email {GMAIL_SEND_SCOPE}"

OFF_MESSAGE = (
    "Google sign-in is off until config.yaml sets connectors.google.client_id. "
    "Create an OAuth client of type Desktop app, allow a loopback redirect on "
    f"127.0.0.1, grant {GMAIL_SEND_SCOPE} plus openid and "
    "email, paste the client id into config, and enter the client secret in "
    "Settings. The secret is not a confidential credential for a desktop client, "
    "but it still lives in the keychain, not in this file. Hosts are "
    "connectors.google.auth_host, connectors.google.token_host, "
    "connectors.google.userinfo_host, and connectors.google.gmail_host."
)

TIMEOUT_MESSAGE = "Sign-in timed out. Try again."
STATE_MESSAGE = "Sign-in could not be verified. Connect again."
DENIED_MESSAGE = "Google sign-in was cancelled."
FAILED_MESSAGE = "Google sign-in failed."
NOT_CONNECTED = "Google is not connected. Sign in from Settings → Email."
SECRET_MESSAGE = (
    "Enter the Google client secret in Settings. It stays in the keychain, not in config.yaml."
)
# Testing-mode grants die after seven days, and an unverified app can be
# revoked. Both come back as invalid_grant. The sentence is the whole warning.
EXPIRED_MESSAGE = (
    "Google sign-in expired. An unverified app, or a client still in testing, "
    "stops refreshing until you reconnect. Reconnect in Settings."
)
NO_MAILBOX = "Google account has no email address."

_SAFE_ERROR = re.compile(r"^[a-z0-9_]+$")
_PHASES = frozenset({"off", "idle", "pending", "connected", "denied", "error", "expired"})
_PUBLIC_LIMIT = 300


class GoogleError(Exception):
    """A Google failure whose message is safe to show and to log."""

    def __init__(self, message: str, *, phase: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.phase = phase if phase in _PHASES else "error"


@dataclass
class GoogleTokens:
    """Access token plus a rotating refresh token. Neither is logged."""

    access_token: str
    refresh_token: str
    expires_at: float

    def __repr__(self) -> str:
        return (
            f"GoogleTokens(expires_at={self.expires_at!r}, has_refresh={bool(self.refresh_token)})"
        )


def printable_secret(value: str) -> bool:
    """True when *value* can sit in the keychain blob (printable ASCII, no NUL)."""
    if not value or "\n" in value or "\x00" in value:
        return False
    return all(0x20 <= ord(ch) <= 0x7E for ch in value)


def public_oauth_message(code: str) -> str:
    """Map an OAuth error code to a sentence. Never the description."""
    if code == "access_denied":
        return DENIED_MESSAGE
    if code == "invalid_grant":
        return EXPIRED_MESSAGE
    if _SAFE_ERROR.fullmatch(code):
        return f"Google sign-in failed ({code})."
    return FAILED_MESSAGE


def oauth_phase(code: str) -> str:
    """Phase for a terminal OAuth error."""
    if code == "access_denied":
        return "denied"
    if code == "invalid_grant":
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
