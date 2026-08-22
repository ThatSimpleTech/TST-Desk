"""M7 exit harness (TD-3806).

Headless mock pass: refuse ``0.0.0.0``, run one due Slack job on
loopback, invoke an injected ``notify_send``. No live tailnet, no Slack
POST, no webhook URL in yaml or logs. This harness is the M7 exit
criterion. Not ``e2e_harness.run``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from .config import EmbeddingsConfig, ModelConfig, Preset, TierConfig
from .daemon import Daemon
from .e2e_checks import HarnessResult, _check
from .e2e_harness import _wait_for_port_file
from .e2e_m5 import _hello
from .mock import MockProvider, Script
from .scheduler.models import Job
from .scheduler.store import jobs_path, list_jobs, save_job
from .ws import validate_interface

_STEERING = "# M7 harness\n\nKeep replies short.\n"
_PROMPT = "m7 scheduled turn"
_REPLY = "m7 mock reply"
_BRAIN = "m7-brain"
_JOB_ID = "m7-slack"
_NOW = datetime(2026, 8, 21, 15, 0, tzinfo=UTC)
_DUE = "2026-08-21T12:00:00+00:00"
_NEXT = "2026-08-21T16:00:00+00:00"
_BUDGET = 60.0
_WEBHOOK_MARKERS = ("hooks.slack.com", "ntfy.sh", "/services/")


def _tier(slug: str) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url="http://127.0.0.1:9/v1",
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=8_000,
        max_output_tokens=1_000,
    )


def m7_config() -> ModelConfig:
    """Known slugs so the mock reply is keyed without discovery."""
    return ModelConfig(
        presets={
            "m7-harness": Preset(brain=_tier(_BRAIN), worker=_tier("m7-w"), validator=_tier("m7-v"))
        },
        active_preset="m7-harness",
        embeddings=EmbeddingsConfig(base_url="", model=""),
    )


def _prepare_workspace(workspace: Path, data_dir: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(_STEERING, encoding="utf-8")


def _seed_job(data_dir: Path, workspace: Path) -> Job:
    return save_job(
        data_dir,
        Job(
            id=_JOB_ID,
            workspace=str(workspace),
            instruction=_PROMPT,
            cadence="every 1 hour",
            next_run=_DUE,
            deliver_to="slack",
        ),
    )


def _scan_webhook_leak(*roots: Path) -> str:
    """Return the first webhook-shaped snippet, or empty if the store is clean."""
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            paths.append(root)
            continue
        paths.extend(root.rglob("*.json"))
        paths.extend(root.rglob("*.yaml"))
        paths.extend(root.rglob("*.yml"))
        paths.extend(root.rglob("*.log"))
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lowered = text.casefold()
        for marker in _WEBHOOK_MARKERS:
            if marker in lowered:
                return f"{path.name}:{marker}"
    return ""


def _refuse_unspecified() -> tuple[bool, str]:
    """``0.0.0.0`` must raise. Never open a socket to prove it."""
    try:
        validate_interface("0.0.0.0")
    except ValueError as exc:
        return True, str(exc)
    return False, "validate_interface accepted 0.0.0.0"


async def _hello_loopback(data_dir: Path) -> dict[str, Any]:
    info = await _wait_for_port_file(data_dir)
    async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
        ack = await _hello(ws, str(info["token"]))
        await ws.close()
    return ack


async def _stop_daemon(daemon: Daemon, task: asyncio.Task[None]) -> None:
    if task.done():
        return
    daemon._shutdown_event.set()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(task, timeout=10.0)
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def run_m7(workspace: Path, data_dir: Path) -> HarnessResult:
    """Drive the M7 exit: refuse unspecified bind, run one Slack job."""
    started = time.monotonic()
    refused, refuse_detail = _refuse_unspecified()
    _prepare_workspace(workspace, data_dir)

    sent: list[tuple[str, str]] = []

    async def notify_send(channel: str, summary: str) -> None:
        sent.append((channel, summary))

    provider = MockProvider(default=Script(kind="stream", content=_REPLY))
    daemon = Daemon(
        data_dir=data_dir,
        provider=provider,
        notify_send=notify_send,
        scheduler_tick=3600.0,
    )
    daemon.config = m7_config()
    daemon_task = asyncio.create_task(daemon.run())
    hello: dict[str, Any] | None = None
    ran: list[str] = []
    again: list[str] = []
    hosts: tuple[str, ...] = ()
    try:
        hello = await _hello_loopback(data_dir)
        hosts = daemon.ws_server.bound_hosts
        _seed_job(data_dir, workspace)
        ran = await daemon.run_due_jobs(_NOW)
        again = await daemon.run_due_jobs(_NOW)
    finally:
        await _stop_daemon(daemon, daemon_task)
    stored = list_jobs(data_dir)
    leak = _scan_webhook_leak(data_dir, jobs_path(data_dir))
    result = HarnessResult(elapsed=time.monotonic() - started)
    result.notes.append("TD-3806 CI green is mock Slack send, not a live webhook")
    _check(result, "bind refused 0.0.0.0", refused, refuse_detail)
    _check(
        result,
        "loopback hello",
        hello is not None and hello.get("type") == "hello_ack",
        f"type={None if hello is None else hello.get('type')}",
    )
    _check(
        result,
        "loopback only",
        hosts == ("127.0.0.1",) and "0.0.0.0" not in hosts,
        f"bound_hosts={hosts!r}",
    )
    _check(
        result,
        "slack job ran once",
        ran == [_JOB_ID] and again == [] and bool(provider.calls),
        f"ran={ran} again={again} calls={len(provider.calls)}",
    )
    _check(
        result,
        "notify_send invoked",
        sent == [("slack", _REPLY)],
        f"sent={sent!r}",
    )
    _check(
        result,
        "job advanced",
        len(stored) == 1 and stored[0].next_run == _NEXT and stored[0].deliver_to == "slack",
        f"next_run={None if not stored else stored[0].next_run}",
    )
    _check(result, "no webhook in store", leak == "", leak or "clean")
    _check(result, "under budget", result.elapsed < _BUDGET, f"{result.elapsed:.1f}s")
    _check(result, "m7 exit criterion", result.ok, "refuse + loopback job + mock Slack send")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="M7 exit harness — mock remote/notify pass (TD-3806)"
    )
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    scratch = tempfile.TemporaryDirectory(prefix="tstd-e2e-m7-")
    root = Path(scratch.name)
    workspace = args.workspace or root / "workspace"
    data_dir = args.data_dir or root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_m7(workspace, data_dir))
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
