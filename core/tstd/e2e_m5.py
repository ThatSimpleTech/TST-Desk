"""M5 exit harness (TD-3204).

The product claim: the session outlives the viewer. This tip does not
have the Tauri hide-on-close commit, so the harness is a protocol
client — the same cut as TD-1401. Closing the viewer is dropping the
WebSocket. The daemon keeps the runner.

Does not go through ``e2e_harness.run``. That pass is frozen.
``tst run`` is the CLI door (TD-3101) against this same mock daemon.
Artifacts (TD-3201) are not on this tip; the ACs do not require one.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from . import cli
from .daemon import Daemon
from .e2e_checks import HarnessResult
from .e2e_harness import _send, _wait_for_port_file
from .mock import MockProvider, Script
from .protocol import PROTOCOL_VERSION

_STEERING = "# M5 harness\n\nKeep replies short.\n"
_FIRST_PROMPT = "first turn with the viewer open"
_SECOND_PROMPT = "second turn after the viewer left"
_CLI_PROMPT = "cli door turn"
_REPLY = "m5 mock reply"
_TURN_TIMEOUT = 15.0
_BUDGET = 60.0
_ALIVE_STATES = frozenset({"running", "idle"})


def _prepare_workspace(workspace: Path, data_dir: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(_STEERING, encoding="utf-8")


async def _recv_event(ws: Any, timeout_secs: float) -> dict[str, Any]:
    """Next JSON object, skipping application pings (TD-1716)."""
    deadline = time.monotonic() + timeout_secs
    while True:
        remaining = max(0.1, deadline - time.monotonic())
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        if isinstance(raw, bytes):
            raw = raw.decode()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError("daemon sent a non-object frame")
        event: dict[str, Any] = parsed
        if event.get("type") == "ping":
            if time.monotonic() >= deadline:
                raise TimeoutError
            continue
        return event


async def _wait_unsubscribed(daemon: Daemon, session_id: str) -> int:
    """Disconnect cleanup is async; wait until this session has no viewers."""
    for _ in range(50):
        n = len(daemon._attached_clients.get(session_id, ()))
        if n == 0:
            return 0
        await asyncio.sleep(0.02)
    return len(daemon._attached_clients.get(session_id, ()))


async def _hello(ws: Any, token: str) -> dict[str, Any]:
    await _send(ws, {"type": "hello", "token": token, "version": PROTOCOL_VERSION})
    ack = await _recv_event(ws, 10.0)
    if ack.get("type") != "hello_ack":
        raise RuntimeError(f"handshake failed: {ack.get('type')}")
    return ack


async def _wait_turn(
    ws: Any, prompt: str, timeout_secs: float
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Wait until ``prompt`` is accepted and that turn completes.

    Replayed ``turn_complete`` events from an earlier turn are ignored.
    """
    seen_user = False
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        event = await _recv_event(ws, max(0.1, deadline - time.monotonic()))
        if event.get("type") == "error":
            return None, event
        if event.get("type") == "user_turn" and event.get("content") == prompt:
            seen_user = True
        if event.get("type") == "turn_complete" and seen_user:
            return event, None
    return None, None


async def _drain_replay(ws: Any, session_id: str, timeout_secs: float) -> list[dict[str, Any]]:
    """Collect the attach burst until two turns are present, then idle-out."""
    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout_secs
    idle = 0.4
    while time.monotonic() < deadline:
        completes = sum(1 for e in events if e.get("type") == "turn_complete")
        wait = idle if completes >= 2 else max(0.1, deadline - time.monotonic())
        try:
            event = await _recv_event(ws, wait)
        except TimeoutError:
            if completes >= 2:
                break
            continue
        if event.get("session_id") == session_id or event.get("type") == "log_trimmed":
            events.append(event)
    return events


def _session_seqs(events: list[dict[str, Any]]) -> list[int]:
    seqs: list[int] = []
    for event in events:
        if event.get("type") == "log_trimmed":
            continue
        seq = event.get("seq")
        if isinstance(seq, int):
            seqs.append(seq)
    return seqs


def _contiguous_from_one(seqs: list[int]) -> bool:
    return bool(seqs) and seqs[0] == 1 and seqs == list(range(1, len(seqs) + 1))


def _check(result: HarnessResult, name: str, ok: bool, detail: str = "") -> None:
    result.checks.append((name, ok, detail))


def _verify(
    *,
    session_id: str,
    first_complete: dict[str, Any] | None,
    listing: dict[str, Any] | None,
    runner_alive: bool,
    session_state: str,
    attached_after_close: int,
    second_complete: dict[str, Any] | None,
    second_error: dict[str, Any] | None,
    replay: list[dict[str, Any]],
    cli_code: int,
    cli_out: str,
    started: float,
) -> HarnessResult:
    result = HarnessResult(elapsed=time.monotonic() - started)
    _check(
        result,
        "first turn",
        first_complete is not None and not first_complete.get("failed"),
        f"type={None if first_complete is None else first_complete.get('type')}",
    )

    row = None
    if listing is not None:
        for item in listing.get("sessions") or []:
            if isinstance(item, dict) and item.get("session_id") == session_id:
                row = item
                break
    listed_state = str(row.get("state") if row else "")
    _check(
        result,
        "viewer left, session stayed",
        row is not None and listed_state in _ALIVE_STATES and attached_after_close == 0,
        f"state={listed_state!r} attached={attached_after_close}",
    )
    _check(
        result,
        "runner still accepts",
        runner_alive and session_state in _ALIVE_STATES,
        f"runner={runner_alive} state={session_state!r}",
    )

    accepted = second_complete is not None and not second_complete.get("failed")
    _check(
        result,
        "second turn after detach",
        accepted and second_error is None,
        f"complete={accepted} error={None if second_error is None else second_error.get('code')}",
    )

    user_turns = [e.get("content") for e in replay if e.get("type") == "user_turn"]
    completes = [e for e in replay if e.get("type") == "turn_complete"]
    seqs = _session_seqs(replay)
    _check(
        result,
        "replay both turns",
        _FIRST_PROMPT in user_turns
        and _SECOND_PROMPT in user_turns
        and len(completes) >= 2
        and all(not c.get("failed") for c in completes[:2]),
        f"user_turns={len(user_turns)} turn_complete={len(completes)}",
    )
    _check(
        result,
        "replay no gaps",
        _contiguous_from_one(seqs),
        f"seqs={seqs[:8]}{'…' if len(seqs) > 8 else ''} n={len(seqs)}",
    )
    _check(
        result,
        "tst run mock",
        cli_code == 0 and _REPLY in cli_out,
        f"exit={cli_code} out={cli_out!r}",
    )
    _check(result, "under budget", result.elapsed < _BUDGET, f"{result.elapsed:.1f}s")
    return result


async def run_m5(workspace: Path, data_dir: Path) -> HarnessResult:
    """Drive the M5 coworker pass: viewer leaves, the session does not."""
    started = time.monotonic()
    _prepare_workspace(workspace, data_dir)

    daemon = Daemon(
        data_dir=data_dir,
        provider=MockProvider(default=Script(kind="stream", content=_REPLY)),
    )
    daemon_task = asyncio.create_task(daemon.run())
    session_id = ""
    first_complete: dict[str, Any] | None = None
    listing: dict[str, Any] | None = None
    runner_alive = False
    session_state = ""
    attached_after_close = -1
    second_complete: dict[str, Any] | None = None
    second_error: dict[str, Any] | None = None
    replay: list[dict[str, Any]] = []
    cli_code = 1
    cli_out = ""

    try:
        info = await _wait_for_port_file(data_dir)
        uri = f"ws://127.0.0.1:{info['port']}"
        token = str(info["token"])

        async with connect(uri) as viewer:
            await _hello(viewer, token)
            await _send(viewer, {"type": "open_workspace", "path": str(workspace)})
            opened = await _recv_event(viewer, 15.0)
            session_id = str(opened.get("session_id") or "")
            await _send(viewer, {"type": "attach", "session_id": session_id, "from_seq": 1})
            await _send(
                viewer,
                {"type": "user_message", "session_id": session_id, "content": _FIRST_PROMPT},
            )
            first_complete, _ = await _wait_turn(viewer, _FIRST_PROMPT, _TURN_TIMEOUT)
            last_seq = 1
            sess = daemon.session_registry.get(session_id)
            if sess is not None:
                last_seq = max(sess.event_log.last_seq, 1)
            # Close the viewer. Do not cancel. Do not shutdown.
            await viewer.close()

        attached_after_close = await _wait_unsubscribed(daemon, session_id)
        found = daemon.session_registry.get(session_id)
        runner = daemon.session_registry.get_runner(session_id)
        session_state = found.state if found is not None else ""
        runner_alive = runner is not None and runner.is_running

        async with connect(uri) as nxt:
            await _hello(nxt, token)
            await _send(nxt, {"type": "list_sessions"})
            listing = await _recv_event(nxt, 10.0)
            await _send(nxt, {"type": "attach", "session_id": session_id, "from_seq": last_seq + 1})
            await _send(
                nxt,
                {"type": "user_message", "session_id": session_id, "content": _SECOND_PROMPT},
            )
            second_complete, second_error = await _wait_turn(nxt, _SECOND_PROMPT, _TURN_TIMEOUT)
            await nxt.close()

        async with connect(uri) as reopen:
            await _hello(reopen, token)
            await _send(reopen, {"type": "attach", "session_id": session_id, "from_seq": 1})
            replay = await _drain_replay(reopen, session_id, _TURN_TIMEOUT)
            await reopen.close()

        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            cli_code = await cli.run_turn(workspace, _CLI_PROMPT, data_dir)
        cli_out = buf.getvalue()
    finally:
        if not daemon_task.done():
            daemon._shutdown_event.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(daemon_task, timeout=10.0)
            if not daemon_task.done():
                daemon_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await daemon_task

    return _verify(
        session_id=session_id,
        first_complete=first_complete,
        listing=listing,
        runner_alive=runner_alive,
        session_state=session_state,
        attached_after_close=attached_after_close,
        second_complete=second_complete,
        second_error=second_error,
        replay=replay,
        cli_code=cli_code,
        cli_out=cli_out,
        started=started,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M5 exit harness — mock coworker pass (TD-3204)")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    scratch = tempfile.TemporaryDirectory(prefix="tstd-e2e-m5-")
    root = Path(scratch.name)
    workspace = args.workspace or root / "workspace"
    data_dir = args.data_dir or root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_m5(workspace, data_dir))
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
