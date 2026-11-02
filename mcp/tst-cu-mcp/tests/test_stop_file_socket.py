"""The stop-file stops input before the host socket is touched (TD-5223).

TST Desk's Stop button now creates this file, so an outside client (one
started from a checkout, acting through the app's ``cu-agent.sock``) must
refuse on it before it opens the socket. The host refuses too, but that is
the second line; this is the first. The socket and ``_cu_transact`` are
spied, so a refusal that happened after a send would still fail here.
"""

from __future__ import annotations

import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tst_cu_mcp import input_control, safety
from tst_cu_mcp.backends import darwin
from tst_cu_mcp.displays import DisplayInfo

ONE_DISPLAY = [
    DisplayInfo(display_id=1, index=0, x=0, y=0, width=1920, height=1080, scale=1.0, is_main=True)
]

ACTIONS: dict[str, Callable[[], None]] = {
    "move_mouse": lambda: input_control.move_mouse(10, 10),
    "click": lambda: input_control.click(10, 10),
    "type_text": lambda: input_control.type_text("hello"),
    "press_keys": lambda: input_control.press_keys("ctrl+c"),
    "scroll": lambda: input_control.scroll(0, 3),
}


@pytest.fixture(autouse=True)
def isolate_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The stop-file lives in a temp dir; never the real ``~/.tst-cu-mcp/STOP``."""
    monkeypatch.setenv(safety.STOP_FILE_ENV, str(tmp_path / "STOP"))
    monkeypatch.delenv(safety.STOP_ENV, raising=False)
    safety.set_config(None)
    yield
    safety.set_config(None)


@pytest.fixture
def socket_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The macOS backend, driving the host socket; every send is recorded."""
    sent: list[str] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(input_control, "list_displays", lambda: ONE_DISPLAY)
    # Not the app itself: an outside client, which acts through the socket.
    monkeypatch.setattr(darwin, "_is_host_identity", lambda: False)

    def transact(line: str, **_: Any) -> bytes:
        sent.append(line)
        return b"ok"

    def no_socket(*_: Any, **__: Any) -> socket.socket:
        sent.append("<socket opened>")
        raise OSError("tests never open a socket")

    monkeypatch.setattr(darwin, "_cu_transact", transact)
    monkeypatch.setattr(socket, "socket", no_socket)
    return sent


class TestStopFileBeforeTheSocket:
    @pytest.mark.parametrize("name", sorted(ACTIONS))
    def test_refused_before_any_socket_call(
        self, name: str, tmp_path: Path, socket_spy: list[str]
    ) -> None:
        (tmp_path / "STOP").write_text("")
        with pytest.raises(safety.KillSwitchEngaged):
            ACTIONS[name]()
        assert socket_spy == []

    @pytest.mark.parametrize("name", sorted(ACTIONS))
    def test_reaches_the_socket_when_not_stopped(self, name: str, socket_spy: list[str]) -> None:
        """The control: without the file the same call does send."""
        ACTIONS[name]()
        assert socket_spy, f"{name} never reached the host socket"
