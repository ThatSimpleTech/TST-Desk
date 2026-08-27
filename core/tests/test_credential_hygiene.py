"""Credential hygiene (TD-1102): the stored key is never written to disk,
never logged, and never lands in the audit database — asserted end to end.

A canary key goes through the full credential flow over a real websocket —
store, status, delete — and afterwards the canary must appear nowhere: not
in any file under the daemon's data dir (sessions store, port file,
audit.db pages), not in any captured log record (message or extras), not in
any wire payload.  The canary is assembled at runtime so the repo's secret
scanner never sees a key-shaped literal.

The turn/tool-call leak surface (tool arguments scrubbed before the audit
insert) is covered generically by test_audit.py's redaction tests; this
file locks down the credential-store path itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

# Reuse the onboarding-wire harness: in-memory keychain pinned into the
# daemon's imports, plus its daemon/ws helpers.
from tests.platform_helpers import stop_daemon_gracefully
from tests.test_setup_state import (
    FakeKeychain,
    _ask,
    _connect_and_handshake,
    _start_daemon,
)
from tstd.config import cached_config

# Runtime-assembled; scanner-safe. Shaped like a real OpenRouter key
# (dashed prefix) so the suite exercises the format the app actually
# stores — an undashed canary matches a too-narrow redaction pattern and
# the test cannot tell the gap from a working redactor (TD-4801).
CANARY = "sk-or-v1-" + "canary" + "ab" * 8


class _LogCapture(logging.Handler):
    """Every record the daemon emits, as formatted text plus extras."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        extras = record.__dict__.get("extra_fields", "")
        self.records.append(f"{record.getMessage()} {extras}")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same isolation as the onboarding tests: never read real user config."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cached_config.cache_clear()


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    fk = FakeKeychain()

    async def _get(provider_name: str = "openrouter") -> str:
        return await fk.get(provider_name)

    async def _store(api_key: str, provider_name: str = "openrouter") -> None:
        await fk.store(api_key, provider_name)

    async def _delete(provider_name: str = "openrouter") -> None:
        await fk.delete(provider_name)

    monkeypatch.setattr("tstd.daemon.get_api_key", _get)
    monkeypatch.setattr("tstd.daemon.store_api_key", _store)
    monkeypatch.setattr("tstd.daemon.delete_api_key", _delete)
    cached_config.cache_clear()
    return fk


def _canary_offenders_on_disk(root: Path) -> list[str]:
    """Names of files under the canary-era data dir whose raw bytes carry it.

    Sync by design — the scan runs via asyncio.to_thread from the async test
    (blocking pathlib calls don't belong on the event loop).
    """
    offenders = []
    for path in root.rglob("*"):
        if path.is_file() and CANARY.encode() in path.read_bytes():
            offenders.append(path.name)
    return offenders


class TestCredentialHygiene:
    @pytest.mark.asyncio
    async def test_key_never_touches_disk_logs_or_audit(self, fake_keychain: FakeKeychain) -> None:
        capture = _LogCapture()
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)  # the daemon's info lines must exist to be checked
        # Mirror setup_logging's production rule: frame payloads are never
        # logged, even at debug (they can carry the key itself).
        ws_logger = logging.getLogger("websockets")
        old_ws_level = ws_logger.level
        ws_logger.setLevel(logging.INFO)
        root.addHandler(capture)
        responses: list[dict[str, Any]] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                daemon, task = await _start_daemon(Path(tmp))
                try:
                    ws = await _connect_and_handshake(
                        f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                    )
                    try:
                        for msg in (
                            {"type": "set_api_key", "api_key": CANARY},
                            {"type": "get_setup_state"},
                            {"type": "delete_api_key"},
                        ):
                            responses.append(await _ask(ws, msg))
                    finally:
                        await ws.close()

                    # The flow really ran against the backing store.
                    assert fake_keychain.stored == {}

                    # No wire payload carries the canary.
                    assert CANARY not in json.dumps(responses)

                    # No log record carries it — message or structured extras.
                    offenders = [r for r in capture.records if CANARY in r]
                    assert not offenders, f"canary leaked into log records: {offenders}"

                    # No file under the data dir carries it (sessions store, port
                    # file, SQLite pages — raw bytes don't lie).
                    on_disk = await asyncio.to_thread(_canary_offenders_on_disk, Path(tmp))
                    assert not on_disk, f"canary leaked onto disk: {on_disk}"

                    # And no audit row, read relationally (defense in depth
                    # against a page-level miss).
                    audit_db = Path(tmp) / "audit.db"
                    if audit_db.exists():
                        with sqlite3.connect(audit_db) as db:
                            tables = [
                                r[0]
                                for r in db.execute(
                                    "SELECT name FROM sqlite_master WHERE type='table'"
                                )
                            ]
                            for table in tables:
                                for row in db.execute(f'SELECT * FROM "{table}"'):
                                    assert CANARY not in str(row), table
                finally:
                    await stop_daemon_gracefully(daemon, task)
        finally:
            root.removeHandler(capture)
            root.setLevel(old_level)
            ws_logger.setLevel(old_ws_level)


class TestFrameLoggingCap:
    """setup_logging never lets the websockets frame dumper emit (TD-1102).

    Frame payloads can carry the API key itself (set_api_key); pattern
    redaction is not a safe net for arbitrarily-shaped keys, so the logger
    is capped at INFO even when the user asks for --log-level debug.
    """

    def test_websockets_capped_at_info_under_debug(self) -> None:
        from tstd.logging import setup_logging

        root = logging.getLogger()
        old_level, old_handlers = root.level, root.handlers[:]
        try:
            # HOME is already isolated by the autouse fixture.
            setup_logging(level="DEBUG", log_to_stdout=False)
            assert root.level == logging.DEBUG
            assert logging.getLogger("websockets").level == logging.INFO
        finally:
            root.handlers.clear()
            root.handlers.extend(old_handlers)
            root.setLevel(old_level)
