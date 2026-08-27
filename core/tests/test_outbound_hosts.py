"""Every outbound destination traces to configuration (TD-1410).

§2.3 — "no telemetry, no analytics, no phone-home, no crash reporting to any
remote; zero network calls the user did not initiate" — was the one prime
directive holding by construction: there is one chat client, its endpoint
comes from config, and discovery refuses a non-loopback endpoint before it
sends.  That is an argument about today's source.  A second client added in
good faith by someone who never read §2 would break the promise silently,
and until this file nothing in the suite would have noticed.

The directive is not "no network".  The user's own ``base_url`` is traffic
the user initiated, and it is the whole point of the product.  The line
that separates the two is *where the destination came from*: configuration,
or a literal somebody typed into the source.  Every check here is that one
question asked a different way.

* **Nothing in the source names a remote host.**  Every URL literal in
  every ``tstd`` module — the tree is enumerated, never hand-listed — must
  be loopback.  A literal whose host is interpolated is not a literal
  destination at all, so it passes: that is config reaching the call site.
* **Only known modules can open a transport.**  A new module importing
  ``httpx``, ``websockets``, ``socket`` or friends fails the confinement
  set, which forces the §2.3 conversation instead of letting a second
  client land quietly.
* **The sending paths land where config points.**  One recorder under
  ``httpx``'s real transport captures every request the provider client,
  model discovery and the key-validation probe make in a full pass, and
  the recorded set must be exactly the configured endpoint.  Point config
  somewhere else and the recording follows; a hardcoded destination would
  not.
* **Importing the package opens nothing.**  Some dependency dialling home
  on import would bypass all of the above, so the import happens in a
  fresh interpreter with the socket layer wired to refuse and record.

Known gap: the source scan reads string literals, so a host assembled from
pieces that never form a URL in any single literal (``"https://" + host``
where ``host`` is itself a bare constant) is not caught by the scan.  The
confinement set and the transport recording are what cover that shape —
the assembled host still needs a client to send it, and that client still
has to land somewhere the recorder can see.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from tstd.config import (
    DiscordNotifyConfig,
    ModelConfig,
    NotifyConfig,
    NtfyNotifyConfig,
    Preset,
    SlackNotifyConfig,
    TelegramNotifyConfig,
    TierConfig,
    cached_config,
    is_loopback_url,
)
from tstd.context.embeddings import EmbeddingsClient
from tstd.daemon import Daemon
from tstd.desktop.grounding_client import GroundingClient
from tstd.desktop.protocol import TINY_PNG
from tstd.discovery import resolve_tier_slugs
from tstd.notify.discord import send as discord_send
from tstd.notify.ntfy import send as ntfy_send
from tstd.notify.slack import send as slack_send
from tstd.notify.telegram import send as telegram_send
from tstd.provider import ChatCompletionRequest, ChatMessage, ProviderClient, RetryConfig

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "tstd"


def _source_files() -> list[Path]:
    """Every Python module under ``tstd``, discovered rather than listed.

    Hand-listing the modules is how this kind of test rots into a
    formality: the file someone adds is exactly the file the list does not
    mention.
    """
    files = sorted(SOURCE_ROOT.rglob("*.py"))
    assert files, f"no sources found under {SOURCE_ROOT}"
    return files


def _parse(path: Path) -> ast.Module:
    # utf-8 explicitly: the platform default is cp1252 on Windows, which
    # cannot decode the UTF-8 punctuation in tstd sources (TD-1406).
    return ast.parse(path.read_text(encoding="utf-8"))


def _relative(path: Path) -> str:
    return path.relative_to(SOURCE_ROOT).as_posix()


# ── 1. No remote host is written into the source ─────────────────────────

# A placeholder standing in for every interpolated part of an f-string.  It
# is a legal hostname, so a URL whose host is interpolated parses to a host
# containing this marker — which is the signal that the destination came
# from a variable rather than from the source.
_INTERPOLATED = "tstdinterpolated"

_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"`<>()\[\],;]+")


def _string_literals(tree: ast.Module) -> list[tuple[int, str]]:
    """Every string literal in *tree*, f-strings with their holes filled.

    Docstrings are included deliberately.  A docstring cannot dial anything
    on its own, but excluding them would carve out a category — and a
    category excluded from a boundary test is where the next remote host
    goes to hide.  The cost is one honest placeholder in ``provider.py``'s
    usage example, which now reads the way the README claims the code
    behaves.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            parts = [
                piece.value
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str)
                else _INTERPOLATED
                for piece in node.values
            ]
            found.append((node.lineno, "".join(parts)))
    return found


def _literal_urls(path: Path) -> list[tuple[int, str, str]]:
    """``(line, url, host)`` for every URL literal with a fixed host."""
    urls: list[tuple[int, str, str]] = []
    for lineno, text in _string_literals(_parse(path)):
        for match in _URL.finditer(text):
            url = match.group(0)
            try:
                host = urlsplit(url).hostname
            except ValueError:  # pragma: no cover - malformed literal
                host = None
            if not host or _INTERPOLATED in host:
                # No host, or a host that came from a variable: not a
                # destination the source decided.
                continue
            urls.append((lineno, url, host))
    return urls


def test_every_url_literal_in_the_source_is_loopback() -> None:
    """A remote destination must come from config, so no source literal may
    name one.  ``is_loopback_url`` is the shipped classifier the product
    itself uses, so the test and the code cannot drift apart on what counts
    as on-box."""
    offenders = [
        f"{_relative(path)}:{lineno} -> {host} ({url})"
        for path in _source_files()
        for lineno, url, host in _literal_urls(path)
        if not is_loopback_url(url)
    ]
    assert not offenders, (
        "remote host hardcoded in the source (§2.3: destinations come from "
        "config, never from a literal):\n  " + "\n  ".join(offenders)
    )


def test_the_literal_scan_actually_reads_the_tree() -> None:
    """Positive control.  A scan that silently found nothing to look at
    would pass forever; the shipped loopback literals prove it parses real
    modules and reaches real strings."""
    seen = {
        (_relative(path), host) for path in _source_files() for _, _, host in _literal_urls(path)
    }
    assert seen, "the URL scan found no literals at all — it is not reading the source"
    assert ("discovery.py", "127.0.0.1") in seen


def test_the_literal_scan_catches_a_planted_remote_host(tmp_path: Path) -> None:
    """The scan's own smoke test: the shapes a hardcoded host arrives in
    must all be caught, and the shapes that are legitimate must not be."""
    caught = tmp_path / "planted.py"
    caught.write_text(
        "import httpx\n"
        'TELEMETRY = "https://telemetry.example.com/v1/events"\n'
        "async def send(c: httpx.AsyncClient) -> None:\n"
        '    await c.post("http://metrics.example.net/ingest")\n',
        encoding="utf-8",
    )
    hosts = {host for _, _, host in _literal_urls(caught)}
    assert hosts == {"telemetry.example.com", "metrics.example.net"}

    allowed = tmp_path / "allowed.py"
    allowed.write_text(
        "def urls(base: str, port: int) -> tuple[str, str]:\n"
        '    return f"{base}/chat/completions", f"ws://127.0.0.1:{port}"\n',
        encoding="utf-8",
    )
    assert all(is_loopback_url(url) for _, url, _ in _literal_urls(allowed))


# ── 2. Only known modules can open a transport ───────────────────────────

# Import roots that can put bytes on a wire.  ``urllib.parse`` and
# ``http.cookies`` are pure parsing, so the check is on the dotted prefix,
# not the top-level package name.
_NETWORK_IMPORTS = (
    "httpx",
    "httpx_sse",
    "websockets",
    "aiohttp",
    "requests",
    "socket",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc",
    "urllib.request",
    "http.client",
)

# asyncio is imported nearly everywhere for concurrency, so importing it
# proves nothing.  These are its actual network entry points.
_ASYNCIO_NETWORK_CALLS = frozenset(
    {
        "open_connection",
        "open_unix_connection",
        "create_connection",
        "create_server",
        "create_unix_connection",
        "start_server",
    }
)

# Every module allowed to reach the network, and the reason it may.
_OUTBOUND_CAPABLE = {
    "provider.py": "the one chat client; base_url is a constructor argument from config",
    "discovery.py": "GET /v1/models, refused before sending unless the endpoint is loopback",
    "ws.py": (
        "the loopback WebSocket server plus opt-in Tailscale bind "
        "(remote.bind); validate_interface() and resolve_remote_bind() "
        "guard the interface — never 0.0.0.0"
    ),
    "daemon.py": "websockets.exceptions for typed disconnects; it serves, it never dials",
    "e2e_harness.py": "the headless harness, a protocol client of our own loopback daemon",
    "benchmarks.py": "the benchmark client, likewise loopback",
    "e2e_live.py": "the opt-in live leg, pointed at an endpoint the developer names",
    "e2e_memory.py": "the M4 memory harness, a protocol client of our own loopback daemon",
    "e2e_m5.py": "the M5 coworker harness, a protocol client of our own loopback daemon",
    "e2e_m6.py": "the M6 exit harness, a protocol client of our own loopback daemon",
    "e2e_m7.py": "the M7 exit harness, a protocol client of our own loopback daemon",
    "e2e_m8.py": (
        "the M8 exit harness; probes and dials only the shipped vllm "
        "preset's loopback /v1 (or a loopback override); off-box is refused "
        "before send"
    ),
    "e2e_m10.py": "the M10 exit harness, a protocol client of our own loopback daemon",
    "cli.py": "tst run; dials only 127.0.0.1 from the daemon port file",
    "tools/web_search.py": "web_search; destination is search.base_url from config",
    "context/embeddings.py": "embeddings; destination is embeddings.base_url from config",
    "desktop/grounding_client.py": (
        "UI-TARS grounding; destination is computer_use.grounding.base_url "
        "from config; loopback-only, empty disables"
    ),
    "notify/slack.py": "slack incoming webhook; destination host is notify.slack.host from config",
    "notify/ntfy.py": "ntfy topic POST; destination host is notify.ntfy.host from config",
    "notify/discord.py": (
        "discord incoming webhook; destination host is notify.discord.host from config"
    ),
    "notify/telegram.py": (
        "telegram sendMessage; destination host is notify.telegram.host from config"
    ),
    "mcp/http.py": (
        "user-listed MCP HTTP client; destination is mcp.servers.<id>.url "
        "from config; loopback-only, refused before send"
    ),
}


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.update(f"{node.module}.{alias.name}" for alias in node.names)
            roots.add(node.module)
    return roots


def _opens_transport(path: Path) -> bool:
    tree = _parse(path)
    for dotted in _imported_roots(tree):
        if any(dotted == root or dotted.startswith(f"{root}.") for root in _NETWORK_IMPORTS):
            return True
    return any(
        isinstance(node, ast.Attribute) and node.attr in _ASYNCIO_NETWORK_CALLS
        for node in ast.walk(tree)
    )


def test_outbound_capable_modules_are_confined() -> None:
    """A second client is the failure mode §2.3 cannot survive, so the set
    of modules that can open one is closed.  Adding a module here is a
    deliberate act with a reason attached — the point is that it cannot
    happen quietly."""
    capable = {_relative(path) for path in _source_files() if _opens_transport(path)}
    unexpected = sorted(capable - set(_OUTBOUND_CAPABLE))
    assert not unexpected, (
        "module gained the ability to open a network transport (§2.3): "
        f"{unexpected}. If it is legitimate, add it to _OUTBOUND_CAPABLE "
        "with the reason its destination traces to config."
    )
    stale = sorted(set(_OUTBOUND_CAPABLE) - capable)
    assert not stale, f"_OUTBOUND_CAPABLE names modules that no longer reach the network: {stale}"


# ── 3. The sending paths land where config points ────────────────────────

# A loopback endpoint on a port nothing else in the tree names, so a
# recorded request either came from this config or from somewhere else
# entirely — there is no coincidence to explain away.
LOCAL_ENDPOINT = "http://127.0.0.1:64110/v1"
# Reserved by RFC 2606: it resolves nowhere, so a leak past the recorder
# cannot reach a real host.
REMOTE_ENDPOINT = "https://configured-endpoint.invalid/v1"

_MODELS_BODY = {"data": [{"id": "sentinel-model", "object": "model"}]}
_COMPLETION_BODY: dict[str, Any] = {
    "id": "cmpl-1410",
    "object": "chat.completion",
    "created": 0,
    "model": "sentinel-model",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_STREAM_BODY = (
    'data: {"id":"cmpl-1410","object":"chat.completion.chunk","created":0,'
    '"model":"sentinel-model","choices":[{"index":0,"delta":{"content":"ok"},'
    '"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


class TransportRecorder:
    """Stands in for httpx's real transport and records where each request
    went.

    Patched onto the transport class rather than injected as a client, so
    it sees every httpx request in the process — including the ones made by
    clients this test never constructed, which is precisely the case a
    hardcoded destination would arrive in.
    """

    def __init__(self) -> None:
        self.urls: list[httpx.URL] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(request.url)
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json=_MODELS_BODY, request=request)
        if path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={"data": [{"embedding": [0.1, 0.2], "index": 0}]},
                request=request,
            )
        if request.headers.get("accept") == "text/event-stream":
            return httpx.Response(
                200,
                content=_STREAM_BODY,
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        return httpx.Response(200, json=_COMPLETION_BODY, request=request)

    def origins(self) -> set[str]:
        found: set[str] = set()
        for url in self.urls:
            netloc = url.netloc
            if isinstance(netloc, bytes):
                netloc = netloc.decode()
            found.add(f"{url.scheme}://{netloc}")
        return found


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> TransportRecorder:
    rec = TransportRecorder()
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        lambda _self, request: rec.handle_async_request(request),
    )
    return rec


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the developer's real config while deciding where the
    daemon sends (mirrors test_credential_hygiene)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cached_config.cache_clear()


def _tier(base_url: str, slug: str | None) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url=base_url,
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=8192,
        max_output_tokens=256,
    )


def _config(base_url: str, slug: str | None) -> ModelConfig:
    preset = Preset(
        brain=_tier(base_url, slug),
        worker=_tier(base_url, slug),
        validator=_tier(base_url, slug),
    )
    return ModelConfig(presets={"sentinel": preset}, active_preset="sentinel")


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="sentinel-model",
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=1,
    )


async def test_every_outbound_destination_traces_to_config(
    recorder: TransportRecorder, tmp_path: Path
) -> None:
    """The enumeration.  Every path that sends is exercised in one pass and
    every destination recorded; the set must be exactly the two endpoints
    config named.  A hardcoded host anywhere along these paths shows up as
    an origin nobody configured.
    """
    # 1. The provider client, both the blocking and the streaming call.
    remote = _config(REMOTE_ENDPOINT, "sentinel-model")
    client = ProviderClient(
        base_url=remote.tier("brain").base_url,
        api_key="sk-td-1410-probe",  # tst-secret-ok
        retry_config=RetryConfig(max_retries=0),
    )
    assert not isinstance(await client.chat_completion(_request()), Exception)
    async for _chunk in client.chat_completion_stream(_request()):
        pass
    await client.close()

    # 2. Model discovery, driven through config the way a turn drives it.
    local = _config(LOCAL_ENDPOINT, None)
    await resolve_tier_slugs(local)
    assert local.tier("brain").slug == "sentinel-model"

    # 3. The key-validation probe, through the daemon that owns it.
    daemon = Daemon(data_dir=tmp_path / "data")
    daemon.config = remote
    validated = await daemon._validate_api_key("sk-td-1410-probe")  # tst-secret-ok
    assert validated.ok, validated.detail

    assert recorder.urls, "no request was recorded — the paths under test never sent"
    assert recorder.origins() == {
        "http://127.0.0.1:64110",
        "https://configured-endpoint.invalid",
    }
    assert {str(u) for u in recorder.urls} == {
        f"{REMOTE_ENDPOINT}/chat/completions",
        f"{LOCAL_ENDPOINT}/models",
    }


async def test_embeddings_destination_traces_to_config(
    recorder: TransportRecorder,
) -> None:
    """The embeddings client lands where embeddings.base_url points."""
    client = EmbeddingsClient(
        base_url="http://127.0.0.1:64111/v1",
        model="nomic-embed-text",
        timeout_seconds=1,
    )
    vectors = await client.embed_or_none(["hello"])
    assert vectors == [[0.1, 0.2]]
    assert recorder.origins() == {"http://127.0.0.1:64111"}
    assert {str(u) for u in recorder.urls} == {"http://127.0.0.1:64111/v1/embeddings"}


async def test_grounding_destination_traces_to_config(
    recorder: TransportRecorder,
) -> None:
    """The grounding client lands where computer_use.grounding.base_url points."""
    client = GroundingClient(
        base_url="http://127.0.0.1:64113/v1",
        slug="sentinel-model",
        timeout_seconds=1,
    )
    await client.locate(TINY_PNG, "Save")
    assert recorder.origins() == {"http://127.0.0.1:64113"}
    assert {str(u) for u in recorder.urls} == {"http://127.0.0.1:64113/v1/chat/completions"}


async def test_slack_destination_traces_to_config(
    recorder: TransportRecorder,
) -> None:
    """Slack notify lands where notify.slack.host points. The webhook URL
    is injected (keychain in production); the host allowlist is config."""
    config = _config(LOCAL_ENDPOINT, "sentinel-model")
    config.notify = NotifyConfig(
        slack=SlackNotifyConfig(enabled=True, host="127.0.0.1", timeout_seconds=1)
    )
    await slack_send(
        config,
        "hello",
        webhook_url="http://127.0.0.1:64112/services/T/B/injected",
    )
    assert recorder.origins() == {"http://127.0.0.1:64112"}
    assert {str(u) for u in recorder.urls} == {"http://127.0.0.1:64112/services/T/B/injected"}


async def test_moving_the_slack_host_moves_the_destination(
    recorder: TransportRecorder,
) -> None:
    """Change notify.slack.host (and the injected URL's host) and the
    request follows. A hardcoded Slack host would not."""
    config = _config(LOCAL_ENDPOINT, "sentinel-model")
    config.notify = NotifyConfig(
        slack=SlackNotifyConfig(enabled=True, host="somewhere-else.invalid", timeout_seconds=1)
    )
    await slack_send(
        config,
        "hello",
        webhook_url="https://somewhere-else.invalid/services/T/B/injected",
    )
    assert recorder.origins() == {"https://somewhere-else.invalid"}


async def test_ntfy_destination_traces_to_config(
    recorder: TransportRecorder,
) -> None:
    """ntfy notify lands where notify.ntfy.host points. The topic URL
    is injected (keychain in production); the host allowlist is config."""
    config = _config(LOCAL_ENDPOINT, "sentinel-model")
    config.notify = NotifyConfig(
        ntfy=NtfyNotifyConfig(enabled=True, host="127.0.0.1", timeout_seconds=1)
    )
    await ntfy_send(
        config,
        "hello",
        topic_url="http://127.0.0.1:64112/desk-topic",
    )
    assert recorder.origins() == {"http://127.0.0.1:64112"}
    assert {str(u) for u in recorder.urls} == {"http://127.0.0.1:64112/desk-topic"}


async def test_moving_the_ntfy_host_moves_the_destination(
    recorder: TransportRecorder,
) -> None:
    """Change notify.ntfy.host (and the injected URL's host) and the
    request follows. A hardcoded ntfy host would not."""
    config = _config(LOCAL_ENDPOINT, "sentinel-model")
    config.notify = NotifyConfig(
        ntfy=NtfyNotifyConfig(enabled=True, host="somewhere-else.invalid", timeout_seconds=1)
    )
    await ntfy_send(
        config,
        "hello",
        topic_url="https://somewhere-else.invalid/desk-topic",
    )
    assert recorder.origins() == {"https://somewhere-else.invalid"}


async def test_discord_destination_traces_to_config(
    recorder: TransportRecorder,
) -> None:
    """Discord notify lands where notify.discord.host points."""
    config = _config(LOCAL_ENDPOINT, "sentinel-model")
    config.notify = NotifyConfig(
        discord=DiscordNotifyConfig(enabled=True, host="127.0.0.1", timeout_seconds=1)
    )
    await discord_send(
        config,
        "hello",
        webhook_url="http://127.0.0.1:64112/api/webhooks/1/injected",
    )
    assert recorder.origins() == {"http://127.0.0.1:64112"}
    assert {str(u) for u in recorder.urls} == {"http://127.0.0.1:64112/api/webhooks/1/injected"}


async def test_moving_the_discord_host_moves_the_destination(
    recorder: TransportRecorder,
) -> None:
    config = _config(LOCAL_ENDPOINT, "sentinel-model")
    config.notify = NotifyConfig(
        discord=DiscordNotifyConfig(enabled=True, host="somewhere-else.invalid", timeout_seconds=1)
    )
    await discord_send(
        config,
        "hello",
        webhook_url="https://somewhere-else.invalid/api/webhooks/1/injected",
    )
    assert recorder.origins() == {"https://somewhere-else.invalid"}


async def test_telegram_destination_traces_to_config(
    recorder: TransportRecorder,
) -> None:
    """Telegram notify lands where notify.telegram.host points."""
    config = _config(LOCAL_ENDPOINT, "sentinel-model")
    config.notify = NotifyConfig(
        telegram=TelegramNotifyConfig(enabled=True, host="127.0.0.1", timeout_seconds=1)
    )
    await telegram_send(
        config,
        "hello",
        bot_url="http://127.0.0.1:64112/botTOKEN/sendMessage?chat_id=1",
    )
    assert recorder.origins() == {"http://127.0.0.1:64112"}
    assert {str(u) for u in recorder.urls} == {"http://127.0.0.1:64112/botTOKEN/sendMessage"}


async def test_moving_the_telegram_host_moves_the_destination(
    recorder: TransportRecorder,
) -> None:
    config = _config(LOCAL_ENDPOINT, "sentinel-model")
    config.notify = NotifyConfig(
        telegram=TelegramNotifyConfig(
            enabled=True, host="somewhere-else.invalid", timeout_seconds=1
        )
    )
    await telegram_send(
        config,
        "hello",
        bot_url="https://somewhere-else.invalid/botTOKEN/sendMessage?chat_id=1",
    )
    assert recorder.origins() == {"https://somewhere-else.invalid"}


async def test_moving_the_configured_endpoint_moves_every_destination(
    recorder: TransportRecorder,
) -> None:
    """Traceability stated as an experiment: change the config value, and
    the destination changes with it.  A literal would not move."""
    moved = _config("https://somewhere-else.invalid/v2", "sentinel-model")
    client = ProviderClient(
        base_url=moved.tier("brain").base_url,
        api_key=None,
        retry_config=RetryConfig(max_retries=0),
    )
    await client.chat_completion(_request())
    await client.close()
    assert recorder.origins() == {"https://somewhere-else.invalid"}


# ── 4. Importing the package opens nothing ───────────────────────────────

_IMPORT_PROBE = """
import json, pkgutil, socket, sys

opened = []


def _refuse(label):
    def hook(*args, **kwargs):
        opened.append(label)
        raise OSError("network refused during the TD-1410 import probe")

    return hook


socket.socket.connect = _refuse("socket.connect")
socket.socket.connect_ex = _refuse("socket.connect_ex")
socket.create_connection = _refuse("socket.create_connection")
socket.getaddrinfo = _refuse("socket.getaddrinfo")

import tstd

failed = {}
for module in pkgutil.walk_packages(tstd.__path__, "tstd."):
    try:
        __import__(module.name)
    except Exception as exc:
        failed[module.name] = f"{type(exc).__name__}: {exc}"

print(json.dumps({"opened": opened, "failed": failed}))
"""


def test_importing_the_package_opens_no_transport() -> None:
    """A dependency that dials on import would sit underneath every other
    check here, so the import runs in a fresh interpreter with the socket
    layer wired to refuse and record."""
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        cwd=SOURCE_ROOT.parent,
        timeout=120,
    )
    assert proc.returncode == 0, f"import probe crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not result["opened"], (
        f"importing tstd opened a network transport (§2.3): {result['opened']}"
    )
    assert not result["failed"], (
        f"a module could not be imported, so it was never checked: {result['failed']}"
    )
