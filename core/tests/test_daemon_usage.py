"""Tests for the usage and export wire (TD-1706).

``get_usage`` answers with one ``usage_report`` carrying every bucket
split by tier; ``export_usage`` reuses the TD-903 exporters and answers
with the path it wrote.  Both read the audit database the running daemon
is writing to, so these also prove a reader can open it while the audit
writer holds its own connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.audit import AuditStore
from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION

WED_TS = datetime(2026, 8, 12, 9, 0, 0).timestamp()
THU_TS = datetime(2026, 8, 13, 9, 0, 0).timestamp()


async def _connect_and_handshake(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _start_daemon(tmp: Path) -> tuple[Daemon, asyncio.Task[Any]]:
    daemon = Daemon(data_dir=tmp)
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop_daemon(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _seed(tmp: Path) -> None:
    """Put priced model calls in the audit database before the daemon starts.

    brain     40,000 in x $3.00/1M + 5,000 out x $15.00/1M = $0.195
    worker    10,000 in x $0.80/1M                         = $0.008
    """
    store = AuditStore(tmp / "audit.db")
    try:
        store.append_session("sess-a", "/ws", WED_TS)
        store.append_session("sess-b", "/ws", THU_TS)
        store.append_model_call(
            session_id="sess-a",
            turn_id=None,
            tier="brain",
            model="test/brain",
            prompt_tokens=40_000,
            cached_prompt_tokens=0,
            completion_tokens=5_000,
            cost=0.195,
            ts=WED_TS,
        )
        store.append_model_call(
            session_id="sess-b",
            turn_id=None,
            tier="worker",
            model="test/worker",
            prompt_tokens=10_000,
            cached_prompt_tokens=0,
            completion_tokens=0,
            cost=0.008,
            ts=THU_TS,
        )
    finally:
        store.close()


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read the export back. Sync on purpose: blocking file calls do not
    belong inside an async test body (ruff ASYNC230/240)."""
    assert path.exists(), f"no export at {path}"
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    assert path.exists(), f"no export at {path}"
    return [json.loads(line) for line in path.read_text().splitlines()]


async def _ask(tmp: Path, message: dict[str, Any]) -> dict[str, Any]:
    daemon, task = await _start_daemon(tmp)
    try:
        ws = await _connect_and_handshake(
            f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
        )
        await ws.send(json.dumps(message))
        resp = dict(json.loads(await ws.recv()))
        await ws.close()
        return resp
    finally:
        await _stop_daemon(task)


@pytest.mark.asyncio
async def test_get_usage_reports_every_bucket_split_by_tier(tmp_path: Path) -> None:
    _seed(tmp_path)
    resp = await _ask(tmp_path, {"type": "get_usage"})

    assert resp["type"] == "usage_report"
    rows = list(resp["rows"])
    assert {r["bucket"] for r in rows} == {"session", "day", "week"}

    sessions = {(r["key"], r["tier"]): r for r in rows if r["bucket"] == "session"}
    assert sessions[("sess-a", "brain")]["cost"] == pytest.approx(0.195)
    assert sessions[("sess-a", "brain")]["prompt_tokens"] == 40_000
    assert sessions[("sess-a", "brain")]["completion_tokens"] == 5_000
    assert sessions[("sess-b", "worker")]["cost"] == pytest.approx(0.008)

    days = {(r["key"], r["tier"]): r for r in rows if r["bucket"] == "day"}
    assert days[("2026-08-12", "brain")]["cost"] == pytest.approx(0.195)
    assert days[("2026-08-13", "worker")]["cost"] == pytest.approx(0.008)

    # Both days fall in the week that opened Monday 2026-08-10.
    weeks = [r for r in rows if r["bucket"] == "week"]
    assert {r["key"] for r in weeks} == {"2026-08-10"}
    assert sum(r["cost"] for r in weeks) == pytest.approx(0.203)


@pytest.mark.asyncio
async def test_get_usage_on_an_empty_store_reports_no_rows(tmp_path: Path) -> None:
    resp = await _ask(tmp_path, {"type": "get_usage"})
    assert resp["type"] == "usage_report"
    assert resp["rows"] == []


@pytest.mark.asyncio
async def test_export_usage_writes_csv_and_reports_the_path(tmp_path: Path) -> None:
    _seed(tmp_path)
    resp = await _ask(tmp_path, {"type": "export_usage", "format": "csv"})

    assert resp["type"] == "usage_exported"
    assert resp["format"] == "csv"
    assert resp["rows"] == 2

    written = Path(resp["path"])
    assert written.parent == tmp_path / "exports"
    rows = _read_csv(written)
    assert [r["session_id"] for r in rows] == ["sess-a", "sess-b"]
    assert float(rows[0]["cost"]) == pytest.approx(0.195)


@pytest.mark.asyncio
async def test_export_usage_writes_jsonl_and_defaults_to_it(tmp_path: Path) -> None:
    _seed(tmp_path)
    resp = await _ask(tmp_path, {"type": "export_usage"})

    assert resp["type"] == "usage_exported"
    assert resp["format"] == "jsonl"
    written = Path(resp["path"])
    assert written.suffix == ".jsonl"
    lines = _read_jsonl(written)
    assert len(lines) == 2
    assert lines[0]["tier"] == "brain"
    # The TD-903 exporter's ISO-8601 UTC stamps, not raw epoch seconds.
    assert lines[0]["ts"].endswith("+00:00")


@pytest.mark.asyncio
async def test_an_unknown_format_is_refused_not_guessed(tmp_path: Path) -> None:
    resp = await _ask(tmp_path, {"type": "export_usage", "format": "parquet"})
    assert resp["type"] == "error"
    assert resp["code"] == "bad_request"
