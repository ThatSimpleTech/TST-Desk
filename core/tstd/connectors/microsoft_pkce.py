"""PKCE S256 and the OAuth state parameter (TD-4853).

A public client has no secret. The verifier proves the token request
came from the process that opened the browser. State ties the loopback
redirect to that same attempt; a mismatch is not exchanged.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets

from .microsoft_models import FAILED_MESSAGE, MicrosoftError

_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


def code_challenge(verifier: str) -> str:
    """BASE64URL(SHA256(verifier)) without padding, as S256 defines it."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_pkce() -> tuple[str, str]:
    """A verifier and its S256 challenge.

    ``token_urlsafe(64)`` is 86 characters, inside the 43-128 window, and
    its alphabet is the unreserved set PKCE allows.
    """
    verifier = secrets.token_urlsafe(64)
    if not _VERIFIER.fullmatch(verifier):
        raise MicrosoftError(FAILED_MESSAGE)
    return verifier, code_challenge(verifier)


def new_state() -> str:
    """Unguessable state for one authorization attempt."""
    return secrets.token_urlsafe(32)


def states_match(expected: str, got: str) -> bool:
    """Constant-time match. A different length is a mismatch, not an error."""
    if not expected or not got or len(expected) != len(got):
        return False
    return secrets.compare_digest(expected, got)
