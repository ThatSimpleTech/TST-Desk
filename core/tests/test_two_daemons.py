"""TD-5015: two real daemons, A (the hub) and B, over 127.0.0.1.

Nothing about the bridge is mocked. Each daemon binds a real loopback
socket; A pairs with B through B's real pairing code and dials B on
``/phone``, which B's test hook counts as remote. Only the dial rule
(127.0.0.1 instead of Tailscale), the Tailscale listener and the
keychain are test doubles. B runs turns on the mock provider.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection

from tests.machine_helpers import (
    FakeKeychain,
    Fleet,
    ask,
    eventually,
    recv_until,
    window,
)
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig, PolicyRule, save_policy
from tstd.remote_attach import load_remote_port

VIEWER = ["machines"]


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    return FakeKeychain().install(monkeypatch)


class _Steps(MockProvider):
    """One script per agent (streaming) call whichever tier the router picks.

    Side calls (the verify judge and the like) are not streamed; they get
    plain text and leave the steps alone.
    """

    def __init__(self, steps: list[Script]) -> None:
        super().__init__(default=Script(kind="stream", content="ok"))
        self._steps = steps
        self._side_call = False

    async def chat_completion(self, request: Any) -> Any:
        # The parent picks its script before its first await: the flag is
        # read (and cleared) by that very call.
        self._side_call = True
        return await super().chat_completion(request)

    def _script_for(self, model: str) -> Script:
        side, self._side_call = self._side_call, False
        if side or not self._steps:
            return super()._script_for(model)
        return self._steps.pop(0)


def _writes(workspace: Path, *names: str) -> MockProvider:
    """Each turn asks to write the next file, then says "done"."""
    save_policy(
        workspace, PolicyConfig(rules=[PolicyRule(tool="fs_write", args="**", effect="ask")])
    )
    steps: list[Script] = []
    for name in names:
        arguments = json.dumps({"path": str(workspace / name), "content": "from A\n"})
        steps.append(Script(kind="tool_call", tool_name="fs_write", tool_arguments=arguments))
        steps.append(Script(kind="stream", content="done"))
    return _Steps(steps)


async def _until(ws: ClientConnection, check: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    return await recv_until(ws, check, within=20)


def _ids(frame: dict[str, Any]) -> set[str]:
    return {row["session_id"] for row in frame["sessions"]}


async def _open(ws: ClientConnection, workspace: Path) -> str:
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    opened = await ask(ws, {"type": "open_workspace", "path": str(workspace)}, "session_state")
    return str(opened["session_id"])


async def _pair(hub: ClientConnection, other: Daemon) -> str:
    """Pair *other* from the hub's window; its mch once it is connected."""
    async with window(other) as screen:
        code = (await ask(screen, {"type": "start_pairing"}, "pairing_code"))["code"]
        port = other.ws_server.port
        await hub.send(
            json.dumps({"type": "add_machine", "host": "127.0.0.1", "port": port, "code": code})
        )
        listing = await _until(
            hub,
            lambda f: (
                f["type"] == "error"
                or (f["type"] == "machine_list" and any(m["port"] == port for m in f["machines"]))
            ),
        )
    assert listing["type"] == "machine_list", listing
    (machine,) = [m for m in listing["machines"] if m["port"] == port]
    await _until(
        hub, lambda f: f["type"] == "machine_list" and _status(f, machine["id"]) == "connected"
    )
    return str(machine["id"])


def _status(frame: dict[str, Any], mch: str) -> str | None:
    return next((m["status"] for m in frame["machines"] if m["id"] == mch), None)


async def _rows_with(hub: ClientConnection, sid: str) -> dict[str, Any]:
    return await _until(hub, lambda f: f["type"] == "session_list" and sid in _ids(f))


def _for(sid: str, kind: str) -> Callable[[dict[str, Any]], bool]:
    return lambda f: f.get("session_id") == sid and f["type"] in (kind, "error")


async def test_a_runs_a_turn_on_b_approves_and_stops_it(
    tmp_path: Path, fleet: Fleet, keychain: FakeKeychain
) -> None:
    work = tmp_path / "ws-b"
    work.mkdir()
    b = await fleet.start(tmp_path / "b", provider=_writes(work, "one.txt", "two.txt"))
    a = await fleet.start(tmp_path / "a")
    async with window(b) as b_screen, window(a, VIEWER) as hub:
        sid = await _open(b_screen, work)
        mch = await _pair(hub, b)
        remote = f"{sid}:{mch}"
        row = next(
            r for r in (await _rows_with(hub, remote))["sessions"] if r["session_id"] == remote
        )
        assert row["machine"] == mch

        await hub.send(json.dumps({"type": "attach", "session_id": remote, "from_seq": 1}))
        await hub.send(json.dumps({"type": "user_message", "session_id": remote, "content": "go"}))
        request = await _until(hub, _for(remote, "approval_request"))
        assert request["type"] == "approval_request", request
        approve = {"type": "approve", "session_id": remote, "tool_call_id": request["tool_call_id"]}
        await hub.send(json.dumps(approve))
        result = await _until(hub, _for(remote, "tool_result"))
        assert result["status"] == "success", result
        await _until(hub, _for(remote, "turn_complete"))
        assert (work / "one.txt").read_text() == "from A\n"

        await hub.send(
            json.dumps({"type": "user_message", "session_id": remote, "content": "more"})
        )
        await _until(hub, _for(remote, "approval_request"))
        await hub.send(json.dumps({"type": "stop_turn", "session_id": remote}))
        await _until(hub, _for(remote, "turn_complete"))
        assert not (work / "two.txt").exists()
        # B's own window saw the whole thing under the raw id.
        await b_screen.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        turn = await _until(b_screen, lambda f: f["type"] == "user_turn")
        assert turn["session_id"] == sid


async def test_removing_b_drops_the_hub_from_b_and_the_key(
    tmp_path: Path, fleet: Fleet, keychain: FakeKeychain
) -> None:
    b = await fleet.start(tmp_path / "b")
    a = await fleet.start(tmp_path / "a")
    async with window(a, VIEWER) as hub:
        mch = await _pair(hub, b)
        assert [d.kind for d in b.remote_devices.summaries()] == ["machine"]
        assert set(keychain.keys) == {mch}
        listing = await ask(hub, {"type": "remove_machine", "machine": mch}, "machine_list")
        assert listing["machines"] == []
        await eventually(lambda: len(b.remote_devices) == 0, within=5)
        left = sorted(keychain.keys)  # ids only: a failure never prints a key
        assert left == []
        assert not (tmp_path / "a" / "computers.yaml").read_text().count(mch)


async def test_mutual_pairing_stays_one_hop(
    tmp_path: Path, fleet: Fleet, keychain: FakeKeychain
) -> None:
    a = await fleet.start(tmp_path / "a")
    b = await fleet.start(tmp_path / "b")
    async with window(a, VIEWER) as on_a, window(b, VIEWER) as on_b:
        sid_a = await _open(on_a, tmp_path / "ws-a")
        sid_b = await _open(on_b, tmp_path / "ws-b")
        mch_b = await _pair(on_a, b)
        mch_a = await _pair(on_b, a)
        seen_a = await _rows_with(on_a, f"{sid_b}:{mch_b}")
        seen_b = await _rows_with(on_b, f"{sid_a}:{mch_a}")
        # Each sees its own rows and the other's own rows, never a second hop.
        for listing in (
            await ask(on_a, {"type": "list_sessions"}, "session_list"),
            await ask(on_b, {"type": "list_sessions"}, "session_list"),
        ):
            assert all(sid.count(":") <= 1 for sid in _ids(listing))
        assert _ids(seen_a) <= {sid_a, f"{sid_b}:{mch_b}"}
        assert _ids(seen_b) <= {sid_b, f"{sid_a}:{mch_a}"}
        final_a = await ask(on_a, {"type": "list_sessions"}, "session_list")
        assert _ids(final_a) == {sid_a, f"{sid_b}:{mch_b}"}


async def test_self_pairing_shows_every_row_twice_and_the_remote_copy_survives(
    tmp_path: Path, fleet: Fleet, keychain: FakeKeychain
) -> None:
    a = await fleet.start(tmp_path / "a")
    async with window(a, VIEWER) as hub:
        sid = await _open(hub, tmp_path / "ws")
        mch = await _pair(hub, a)
        remote = f"{sid}:{mch}"
        listing = await ask(hub, {"type": "list_machines"}, "machine_list")
        assert listing["machines"][0]["same_computer"] is True
        await _rows_with(hub, remote)

        await hub.send(json.dumps({"type": "attach", "session_id": remote, "from_seq": 1}))
        await _until(hub, _for(remote, "session_state"))
        # A lifecycle verb on the local copy answers with the merged list.
        renamed = await ask(
            hub, {"type": "rename_session", "session_id": sid, "title": "Mine"}, "session_list"
        )
        assert {sid, remote} <= _ids(renamed)
        # The pane bound to the remote copy still streams under its own id.
        await hub.send(json.dumps({"type": "user_message", "session_id": sid, "content": "hi"}))
        echoed = await _until(hub, _for(remote, "user_turn"))
        assert echoed["type"] == "user_turn"
        later = await _rows_with(hub, remote)
        titles = {r["session_id"]: r.get("title") for r in later["sessions"]}
        assert titles[sid] == "Mine"


async def test_a_restarted_hub_reconnects_from_the_keychain(
    tmp_path: Path, fleet: Fleet, keychain: FakeKeychain
) -> None:
    b = await fleet.start(tmp_path / "b")
    a = await fleet.start(tmp_path / "a")
    async with window(b) as b_screen:
        sid = await _open(b_screen, tmp_path / "ws-b")
    async with window(a, VIEWER) as hub:
        mch = await _pair(hub, b)
        await _rows_with(hub, f"{sid}:{mch}")
    await fleet.stop(a)
    reads = keychain.reads
    again = await fleet.start(tmp_path / "a")
    async with window(again, VIEWER) as hub:
        first = await _until(hub, lambda f: f["type"] == "machine_list")
        assert [m["id"] for m in first["machines"]] == [mch]
        await _until(hub, lambda f: f["type"] == "machine_list" and _status(f, mch) == "connected")
        await _rows_with(hub, f"{sid}:{mch}")
    assert keychain.reads > reads


async def test_a_restarted_b_resumes_the_seq_with_no_gap(
    tmp_path: Path, fleet: Fleet, keychain: FakeKeychain
) -> None:
    work = tmp_path / "ws-b"
    reply = Script(kind="stream", content="hello")
    b = await fleet.start(tmp_path / "b", provider=MockProvider(default=reply))
    a = await fleet.start(tmp_path / "a")
    async with window(a, VIEWER) as hub:
        async with window(b) as b_screen:
            sid = await _open(b_screen, work)
        mch = await _pair(hub, b)
        b_port = b.ws_server.port
        assert load_remote_port(tmp_path / "b") == b_port
        remote = f"{sid}:{mch}"
        await _rows_with(hub, remote)
        seqs: list[int] = []
        await hub.send(json.dumps({"type": "attach", "session_id": remote, "from_seq": 1}))
        await hub.send(json.dumps({"type": "user_message", "session_id": remote, "content": "1"}))
        await _collect(hub, remote, seqs, _for(remote, "turn_complete"))
        before = len(seqs)

        await fleet.stop(b)
        await _collect(hub, remote, seqs, _machine(mch, lambda s: s != "connected"))
        b = await fleet.start(tmp_path / "b", provider=MockProvider(default=reply))
        assert b.ws_server.port == b_port
        await _collect(hub, remote, seqs, _machine(mch, lambda s: s == "connected"))
        await hub.send(json.dumps({"type": "user_message", "session_id": remote, "content": "2"}))
        await _collect(hub, remote, seqs, _for(remote, "turn_complete"))
        # Every frame once, in order, across the restart: no gap, no repeat.
        assert len(seqs) > before
        assert seqs == list(range(1, len(seqs) + 1))


def _machine(mch: str, ok: Callable[[str | None], bool]) -> Callable[[dict[str, Any]], bool]:
    return lambda f: f["type"] == "machine_list" and ok(_status(f, mch))


async def _collect(
    hub: ClientConnection,
    sid: str,
    seqs: list[int],
    stop: Callable[[dict[str, Any]], bool],
) -> None:
    """Read until *stop*, keeping the seq of every frame for *sid* on the way."""
    while True:
        frame = await _until(hub, lambda _f: True)
        if frame.get("session_id") == sid:
            assert frame["type"] != "error", frame
            if "seq" in frame:
                seqs.append(frame["seq"])
        if stop(frame):
            return
