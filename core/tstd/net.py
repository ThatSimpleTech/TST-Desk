"""Where a URL points, and how to dial this computer (TD-5181).

A leaf module (no ``tstd`` imports) so the chat client can use it too:
``config`` imports ``provider``, so ``provider`` cannot import ``config``.

httpx applies ``HTTP_PROXY``, ``HTTPS_PROXY`` and ``ALL_PROXY`` (and the
macOS or Windows system proxy) to every URL that ``NO_PROXY`` does not
list, ``127.0.0.1`` and ``localhost`` included. The proxy may be another
machine, so a request meant for a server on this computer, carrying a
prompt, a recording or a screenshot, would leave it. :func:`direct_transport`
gives a loopback URL an explicit transport, which makes httpx skip its proxy
lookup. Unlike ``trust_env=False`` it keeps ``SSL_CERT_FILE`` (TD-5110). A
hosted URL gets ``None``: the person's proxy still carries traffic that is
meant to leave.
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx


def is_loopback_url(url: str) -> bool:
    """True when *url* points at this machine (127.0.0.0/8, ``::1``, ``localhost``).

    A loopback endpoint is on-box by construction, so there is no third party
    to authenticate against and no credential to send (TD-1801). Anything we
    cannot confidently classify — no scheme, an unparseable host, a name that
    merely looks local — is treated as remote, so an ambiguous URL keeps the
    key requirement rather than silently dropping it.

    Deliberately separate from ``ws.validate_interface``: that guards which
    interface we *bind* (§2.1), this classifies an endpoint we *call*.
    """
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def direct_transport(url: str) -> httpx.AsyncHTTPTransport | None:
    """A proxy-free transport for a loopback *url*, else ``None``.

    Pass the result as ``transport=`` when making a client for one
    destination. ``None`` is httpx's default, so a hosted URL behaves
    exactly as before.
    """
    return httpx.AsyncHTTPTransport() if is_loopback_url(url) else None
