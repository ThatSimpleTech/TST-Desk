"""TD-4832: focus_window broadcast after the last CU episode closes.

The daemon emits ``focus_window`` live when a ``cu_session`` closes and
no other session still actuates. The event is connection-scoped and
never written to a session log, so attach replay cannot re-trigger it.
"""

from __future__ import annotations

import json
from pathlib import Path

from tstd.daemon import Daemon
from tstd.protocol import FocusWindow


class _Registry:
    """Minimal stand-in for the session registry."""

    def __init__(self, rows: list[tuple[str, bool]]) -> None:
        self._rows = rows

    async def list_sessions(self) -> list[object]:
        return [
            type("S", (), {"id": sid, "cu_session_active": active}) for sid, active in self._rows
        ]


class _Ws:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail = fail

    async def broadcast(self, payload: str) -> int:
        if self._fail:
            raise RuntimeError("socket gone")
        self.sent.append(payload)
        return 1


def _daemon(tmp_path: Path, rows: list[tuple[str, bool]], *, fail: bool = False) -> Daemon:
    daemon = Daemon(data_dir=tmp_path / "data")
    daemon.ws_server = _Ws(fail=fail)  # type: ignore[assignment]
    daemon.session_registry = _Registry(rows)  # type: ignore[assignment]
    return daemon


class TestCuFocusBroadcast:
    async def test_broadcasts_when_no_session_actuates(self, tmp_path: Path) -> None:
        daemon = _daemon(tmp_path, [("sess-1", False)])
        await daemon._maybe_broadcast_cu_focus("sess-1")
        assert len(daemon.ws_server.sent) == 1  # type: ignore[attr-defined]
        payload = json.loads(daemon.ws_server.sent[0])  # type: ignore[attr-defined]
        assert payload["type"] == "focus_window"
        assert payload["reason"] == "cu_session_closed"

    async def test_defers_while_another_session_actuates(self, tmp_path: Path) -> None:
        daemon = _daemon(tmp_path, [("sess-1", False), ("sess-2", True)])
        await daemon._maybe_broadcast_cu_focus("sess-1")
        assert daemon.ws_server.sent == []  # type: ignore[attr-defined]

    async def test_broadcast_failure_never_raises(self, tmp_path: Path) -> None:
        daemon = _daemon(tmp_path, [], fail=True)
        await daemon._maybe_broadcast_cu_focus("sess-1")  # no raise

    def test_event_is_connection_scoped_so_replay_cannot_carry_it(self) -> None:
        event = FocusWindow()
        assert event.seq == 1
        assert "session_id" not in FocusWindow.model_fields
