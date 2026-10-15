"""Git credential helper. Speaks the helper protocol on stdin/stdout.

Git runs this as ``!python -m tstd.connectors.github_credential``. A
password already on stdin (store/approve) is ignored and never written
down. A token is printed only when the host is the configured web host.
Failures exit 1 with no traceback and no token (TD-4852).
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

from .github_models import GithubError


def _parse(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip().lower()] = value
    return fields


def _host_matches(value: str, web_host: str) -> bool:
    host = value.strip()
    if host.casefold() == web_host.casefold():
        return True
    name, _sep, port = host.rpartition(":")
    return bool(port.isdigit() and name.casefold() == web_host.casefold())


def credential_answer(stdin_text: str, *, web_host: str, token: str) -> str:
    """The helper's stdout, or empty when this fill must not supply a token."""
    if not web_host or not token:
        return ""
    fields = _parse(stdin_text)
    if "password" in fields:
        return ""
    protocol = fields.get("protocol", "")
    if protocol.casefold() != "https":
        return ""
    url = fields.get("url", "")
    if url:
        parts = urlsplit(url)
        if parts.username or parts.password:
            return ""
        if parts.scheme and parts.scheme.casefold() != "https":
            return ""
        if parts.hostname and not _host_matches(parts.hostname, web_host):
            return ""
    if not _host_matches(fields.get("host", ""), web_host):
        return ""
    return f"username=x-access-token\npassword={token}\n"


def _config_from_env() -> tuple[str, str, str, float]:
    import os

    from ..config import GithubConnectorConfig

    raw = os.environ.get("TST_GITHUB_TIMEOUT", "")
    try:
        timeout = float(raw) if raw else 20.0
    except ValueError:
        timeout = 20.0
    cfg = GithubConnectorConfig(
        client_id=os.environ.get("TST_GITHUB_CLIENT_ID", ""),
        api_host=os.environ.get("TST_GITHUB_API_HOST", ""),
        web_host=os.environ.get("TST_GITHUB_WEB_HOST", ""),
        timeout_seconds=timeout if timeout > 0 else 20.0,
    )
    return cfg.web_host, cfg.api_host, cfg.client_id, cfg.timeout_seconds


async def _load_token() -> str:
    import os

    from ..config import GithubConnectorConfig
    from .github_tokens import KeychainTokenStore, ensure_access

    web, api, client_id, timeout = _config_from_env()
    cfg = GithubConnectorConfig(
        client_id=client_id,
        api_host=api,
        web_host=web,
        timeout_seconds=timeout,
    )
    if not os.environ.get("TST_GITHUB_WEB_HOST"):
        raise GithubError("GitHub sign-in failed.")
    return await ensure_access(cfg, KeychainTokenStore())


def main(stdin_text: str | None = None) -> int:
    """Entry point. Swallows errors so git never sees a traceback or a token."""
    import asyncio

    try:
        text = sys.stdin.read() if stdin_text is None else stdin_text
        web_host = _config_from_env()[0]
        token = asyncio.run(_load_token())
        sys.stdout.write(credential_answer(text, web_host=web_host, token=token))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
