"""GitHub connector values that are safe to pass around.

Tokens and the device code are secrets. Their ``repr`` omits them so a
log of the object cannot leak what the keychain is for (TD-4852).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

# Shown in Settings when the connector cannot run. No host, no client id.
OFF_MESSAGE = (
    "GitHub is off until config.yaml sets connectors.github.client_id. "
    "Create a GitHub App, turn on Device Flow (no client secret, no callback URL), "
    "and paste that App's client id. The hosts are connectors.github.api_host "
    "and connectors.github.web_host."
)

_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_SHA_RE = re.compile(r"^[A-Fa-f0-9]{7,40}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
_SAFE_ERROR = re.compile(r"^[a-z0-9_]+$")
_USER_CODE_MAX = 64


class GithubError(Exception):
    """A GitHub failure whose message is safe to show and to log."""

    def __init__(self, message: str, *, phase: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.phase = phase


@dataclass
class GithubTokens:
    """User-to-server access token plus a rotating refresh token."""

    access_token: str
    refresh_token: str
    expires_at: float

    def __repr__(self) -> str:
        return (
            f"GithubTokens(expires_at={self.expires_at!r}, has_refresh={bool(self.refresh_token)})"
        )


@dataclass
class DeviceCode:
    """The device-flow challenge. ``device_code`` never leaves the daemon."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int

    def __repr__(self) -> str:
        return (
            f"DeviceCode(user_code={self.user_code!r}, expires_in={self.expires_in},"
            f" interval={self.interval})"
        )


def public_oauth_message(code: str) -> str:
    """Map a GitHub OAuth error code to a sentence. Never the description."""
    if code == "expired_token":
        return "The code expired. Connect again."
    if code == "access_denied":
        return "GitHub sign-in was denied."
    if _SAFE_ERROR.fullmatch(code):
        return f"GitHub sign-in failed ({code})."
    return "GitHub sign-in failed."


def oauth_phase(code: str) -> str:
    """Phase for a terminal OAuth error. Pending and slow_down are not terminal."""
    if code == "expired_token":
        return "expired"
    if code == "access_denied":
        return "denied"
    return "error"


def printable_secret(value: str) -> bool:
    """True when *value* can sit in the keychain blob (printable ASCII, no NUL)."""
    if not value or "\n" in value or "\x00" in value:
        return False
    return all(0x20 <= ord(ch) <= 0x7E for ch in value)


def require_owner(value: str) -> str:
    if not isinstance(value, str) or not _OWNER_RE.fullmatch(value):
        raise GithubError("Owner or repo is not a valid name.")
    return value


def require_branch(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _BRANCH_RE.fullmatch(value)
        or ".." in value
        or value.startswith(("/", "-"))
        or value.endswith("/")
        or "@{" in value
    ):
        raise GithubError("Branch name is not valid.")
    return value


def require_ref(value: str) -> str:
    if isinstance(value, str) and _SHA_RE.fullmatch(value):
        return value
    return require_branch(value)


def require_path(value: str) -> str:
    if not isinstance(value, str) or value.startswith("/") or "\\" in value:
        raise GithubError("Path is not valid.")
    parts = value.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise GithubError("Path is not valid.")
    return value


def quote_path(value: str) -> str:
    require_path(value)
    return "/".join(quote(part, safe="") for part in value.split("/"))


def quote_branch(value: str) -> str:
    require_branch(value)
    return quote(value, safe="/")


def require_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000_000:
        raise GithubError("That number is not valid.")
    return value


def require_login(value: object) -> str | None:
    if isinstance(value, str) and _LOGIN_RE.fullmatch(value):
        return value
    return None


def check_user_code(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _USER_CODE_MAX:
        raise GithubError("GitHub sign-in failed.")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) > 0x7E for ch in value):
        raise GithubError("GitHub sign-in failed.")
    return value


def check_verification_uri(value: object, web_host: str) -> str:
    """Accept only https on the configured web host. The URI is shown, not logged."""
    if not isinstance(value, str) or not value:
        raise GithubError("GitHub sign-in failed.")
    parts = urlsplit(value)
    host = (parts.hostname or "").casefold()
    if parts.scheme != "https" or parts.username or parts.password or host != web_host.casefold():
        raise GithubError("GitHub sign-in failed.")
    return value


def positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value > 0 and value.is_integer():
        return int(value)
    return None
