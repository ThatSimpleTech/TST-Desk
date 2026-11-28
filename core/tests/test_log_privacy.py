"""The daemon log holds no URL query, no URL credential, and is owner-only (LIVE2-01, LIVE-08).

On the real install the log held 3,245 httpx lines: every web search's
words, fetched page addresses with their parameters, and the LAN and tailnet
addresses of the user's model servers. httpx and httpcore are capped at
WARNING, the app logs a request as method, host and path, query strings are
cut from every log line, user info is redacted on every output path, and the
log file is 0600.
"""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import httpx
import pytest

from tstd.audit import AuditStore
from tstd.logging import (
    LOG_FILE_NAME,
    QUERY_REMOVED,
    REDACTED,
    redact_secrets,
    setup_logging,
    strip_url_queries,
)
from tstd.net_log import REQUEST_HOOKS, request_line


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    setup_logging(level="INFO", log_dir=tmp_path)
    yield tmp_path / LOG_FILE_NAME
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)


def _lines(path: Path) -> list[dict[str, object]]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_httpx_and_httpcore_say_nothing_below_warning(log_file: Path) -> None:
    logging.getLogger("httpx").info("HTTP Request: GET https://search.example/?q=my+words")
    logging.getLogger("httpcore.http11").debug("send_request_headers")
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    assert not any("my+words" in json.dumps(line) for line in _lines(log_file))


async def test_a_request_is_logged_as_host_and_path(log_file: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), event_hooks=REQUEST_HOOKS
    ) as client:
        await client.get("https://me:hunter2@search.example:8443/find?q=private+words#frag")
    assert seen  # the request itself is unchanged
    [line] = [x for x in _lines(log_file) if x["message"] == "http request"]
    assert line["method"] == "GET"
    assert line["url"] == "https://search.example:8443/find"
    text = json.dumps(_lines(log_file))
    assert "private" not in text and "hunter2" not in text


def test_any_log_line_loses_its_url_queries(log_file: Path) -> None:
    log = logging.getLogger("tstd.example")
    log.info(
        "fetched https://docs.example/share?token=abc&u=1 then http://10.0.0.5:8000/v1?x=2",
        extra={"extra_fields": {"url": "https://a.example/p?q=secret", "n": 3}},
    )
    [line] = [x for x in _lines(log_file) if x["logger"] == "tstd.example"]
    assert line["message"] == (
        f"fetched https://docs.example/share{QUERY_REMOVED} then "
        f"http://10.0.0.5:8000/v1{QUERY_REMOVED}"
    )
    assert line["url"] == f"https://a.example/p{QUERY_REMOVED}" and line["n"] == 3


def test_user_info_is_redacted_everywhere_but_queries_only_in_the_log() -> None:
    assert redact_secrets("clone https://user:pass@git.example/r.git") == (
        f"clone https://{REDACTED}@git.example/r.git"
    )
    assert redact_secrets("https://tok123@host.example/") == f"https://{REDACTED}@host.example/"
    # What the agent reads keeps its links whole.
    assert redact_secrets("https://www.example.com/watch?v=abc") == (
        "https://www.example.com/watch?v=abc"
    )
    assert strip_url_queries("see https://x.example/a?b=c.") == (
        f"see https://x.example/a{QUERY_REMOVED}"
    )
    assert strip_url_queries("no url here?q=1") == "no url here?q=1"


def test_request_line_keeps_only_scheme_host_port_and_path() -> None:
    assert request_line(httpx.URL("http://127.0.0.1:11434/v1/chat?x=1")) == (
        "http://127.0.0.1:11434/v1/chat"
    )


def test_the_log_and_the_audit_database_are_owner_only(log_file: Path, tmp_path: Path) -> None:
    logging.getLogger("tstd.example").info("hello")
    _lines(log_file)
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
    store = AuditStore(tmp_path / "audit.db")
    try:
        assert stat.S_IMODE((tmp_path / "audit.db").stat().st_mode) == 0o600
    finally:
        store.close()
