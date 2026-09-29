"""Re-read a provider key after the cached client is rejected (TD-4840).

``Daemon._clients`` keeps one handle per ``(base_url, credential)``. The
handle's HTTP client is built with the key read at that moment, and an
in-app key change is the only thing that used to drop it. A key fixed
or rotated outside the app stayed in the handle until the process
restarted, so later turns kept sending the stale bearer.

An authentication failure closes that HTTP client and builds a new one
from the keychain. The call is repeated once, and only when a digest of
the key changed. The same key would be rejected again. A second
rejection, after the key did change, is the same ``api_key_rejected``
sentence as an unchanged key: the provider's own ``auth_failed`` would
put the generic 401 banner in the window. There is no third try. The
digest is compared in memory and never logged: it is still an oracle
for a low-entropy secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol
from urllib.parse import urlsplit

from .keychain import KeychainError
from .logging import get_logger
from .provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderError,
    StreamChunk,
)

log = get_logger("tstd.provider_refresh")

# Wire code for "the keychain still holds the key the provider just
# rejected". Distinct from ``auth_failed`` so the UI can point at
# Settings → API keys instead of the generic 401 copy.
API_KEY_REJECTED = "api_key_rejected"

_AUTH_CODES = frozenset({"auth_failed", "forbidden"})
_AUTH_STATUS = frozenset({401, 403})


class _ChatClient(Protocol):
    """The two calls a provider handle must forward.

    ``chat_completion_stream`` is an async generator on every real
    implementation. Calling it returns an async iterator directly.
    """

    def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]: ...

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError: ...


Rebuild = Callable[[], Awaitable[_ChatClient]]


def is_auth_failure(error: ProviderError) -> bool:
    """True for the statuses and codes ``provider.py`` uses for 401 and 403."""
    return error.status_code in _AUTH_STATUS or error.code in _AUTH_CODES


def auth_status(error: ProviderError) -> int:
    """HTTP status to show the user. A code with no status still names one."""
    if error.status_code in _AUTH_STATUS:
        return error.status_code
    if error.code == "forbidden":
        return 403
    return 401


def key_digest(api_key: str | None) -> str:
    """SHA-256 hex of *api_key*, or ``""`` when there is nothing to send.

    Callers compare this instead of the key. Do not log it.
    """
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _digest_of(client: object) -> str:
    key = getattr(client, "api_key", None)
    if not isinstance(key, str):
        return ""
    return key_digest(key)


def _same_digest(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def host_label(base_url: str) -> str:
    """``host`` or ``host:port`` for a user-facing message. No path, no secret."""
    parts = urlsplit(base_url)
    host = parts.hostname
    if not host:
        return base_url
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        return f"{host}:{parts.port}"
    return host


def rejected_key_message(credential_name: str, base_url: str, status: int) -> str:
    """The sentence a turn shows when the stored key was rejected."""
    return (
        f"{credential_name} ({host_label(base_url)}) rejected the API key "
        f"({status}) — check it in Settings → API keys"
    )


def _rejected(credential_name: str, base_url: str, error: ProviderError) -> ProviderError:
    status = auth_status(error)
    return ProviderError(
        code=API_KEY_REJECTED,
        message=rejected_key_message(credential_name, base_url, status),
        status_code=status,
        retryable=False,
    )


def _or_rejected(credential_name: str, base_url: str, error: ProviderError) -> ProviderError:
    """Auth failures share one window code. Anything else is passed through.

    ``auth_failed`` is the generic 401 copy. The window picks its banner
    from the code, so a retry that is also rejected has to carry
    ``api_key_rejected`` or the host sentence never reaches the user.
    """
    if is_auth_failure(error):
        return _rejected(credential_name, base_url, error)
    return error


async def _aclose(stream: object) -> None:
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _close_client(client: object) -> None:
    """Close *client* if it owns a pool. Never raise, never log a secret."""
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        # An httpx message can echo the Authorization value. The type is enough.
        log.warning(
            "closing provider client failed",
            extra={"extra_fields": {"error_type": type(exc).__name__}},
        )


class RefreshingProvider:
    """A cached provider handle that swaps its HTTP client after a rejected key.

    The session loop holds this object for the life of the session, so
    replacing the daemon's cache entry alone would leave the stale client
    in place. The HTTP client inside is what gets closed and rebuilt.
    """

    def __init__(
        self,
        inner: _ChatClient,
        *,
        credential_id: str,
        credential_name: str,
        base_url: str,
        rebuild: Rebuild,
    ) -> None:
        self._inner: _ChatClient | None = inner
        self._digest = _digest_of(inner)
        self._credential_id = credential_id
        self._credential_name = credential_name
        self._base_url = base_url
        self._rebuild = rebuild
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """Close the current HTTP client. Tests and shutdown use this."""
        async with self._lock:
            inner = self._inner
            self._inner = None
            self._digest = ""
            if inner is not None:
                await _close_client(inner)

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError:
        ready = await self._ready()
        if isinstance(ready, ProviderError):
            return ready
        client, digest = ready
        result = await client.chat_completion(request)
        if not isinstance(result, ProviderError) or not is_auth_failure(result):
            return result
        outcome = await self._after_auth(digest, result)
        if isinstance(outcome, ProviderError):
            return outcome
        retried = await outcome.chat_completion(request)
        if isinstance(retried, ProviderError):
            return _or_rejected(self._credential_name, self._base_url, retried)
        return retried

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        ready = await self._ready()
        if isinstance(ready, ProviderError):
            yield ready
            return
        client, digest = ready
        auth: ProviderError | None = None
        yielded = False
        stream = client.chat_completion_stream(request)
        try:
            async for item in stream:
                if not yielded and isinstance(item, ProviderError) and is_auth_failure(item):
                    auth = item
                    break
                yielded = True
                yield item
        finally:
            await _aclose(stream)
        if auth is None:
            return
        outcome = await self._after_auth(digest, auth)
        if isinstance(outcome, ProviderError):
            yield outcome
            return
        # One retry. A second rejection uses the same typed failure as an
        # unchanged key, so the window does not fall back to generic 401 copy.
        retry = outcome.chat_completion_stream(request)
        try:
            async for item in retry:
                if isinstance(item, ProviderError):
                    yield _or_rejected(self._credential_name, self._base_url, item)
                else:
                    yield item
        finally:
            await _aclose(retry)

    async def _ready(self) -> tuple[_ChatClient, str] | ProviderError:
        async with self._lock:
            if self._inner is None:
                try:
                    self._inner = await self._rebuild()
                except KeychainError as exc:
                    self._digest = ""
                    return ProviderError(
                        code="missing_api_key",
                        message=str(exc),
                        retryable=False,
                    )
                self._digest = _digest_of(self._inner)
            return self._inner, self._digest

    async def _after_auth(
        self, failed_digest: str, error: ProviderError
    ) -> _ChatClient | ProviderError:
        """Close the client that failed and re-read the keychain.

        Returns the replacement client when the key changed, so the caller
        can make exactly one more attempt. Returns a typed error when the
        key did not change, or when the keychain no longer has one.
        """
        async with self._lock:
            if self._inner is not None and not _same_digest(self._digest, failed_digest):
                # Another call already swapped in a new key.
                return self._inner
            old = self._inner
            self._inner = None
            if old is not None:
                await _close_client(old)
            try:
                new = await self._rebuild()
            except KeychainError as exc:
                self._digest = ""
                log.warning(
                    "provider key re-read failed",
                    extra={
                        "extra_fields": {
                            "credential": self._credential_id,
                            "host": host_label(self._base_url),
                            "error_type": type(exc).__name__,
                        }
                    },
                )
                return ProviderError(
                    code="missing_api_key",
                    message=str(exc),
                    retryable=False,
                )
            new_digest = _digest_of(new)
            changed = not _same_digest(new_digest, failed_digest)
            self._inner = new
            self._digest = new_digest
            log.info(
                "provider authentication failed",
                extra={
                    "extra_fields": {
                        "credential": self._credential_id,
                        "host": host_label(self._base_url),
                        "status_code": auth_status(error),
                        "key_changed": changed,
                    }
                },
            )
            if not changed:
                return _rejected(self._credential_name, self._base_url, error)
            return new
