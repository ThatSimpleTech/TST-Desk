"""Windows shell-kill path, driven on Linux through a taskkill shim (TD-1406).

Hosted Actions on this repo does not assign runners, so the win32 pytest
leg has never executed the unskipped tests. This module forces the
Windows branch (`sys.platform`, the Python grandchild probe, `_kill_windows_tree`)
and supplies a `/proc`-walking `taskkill` that honours `/T /F /PID`.

It is not a substitute for a real Windows host — `CREATE_NEW_PROCESS_GROUP`
is 0 here, and the shim is not Microsoft's `taskkill`. It is the strongest
tree-kill exercise this machine can perform, and it would have caught the
TerminateProcess-first reparent bug.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from tests.test_shell_tools import (
    _KILL_REFUSED,
    _assert_group_gone,
    _wait_for_file,
    escape_probe_cmd,
    make_session,
    make_shell_dispatcher,
)

_SHIM = r"""#!/usr/bin/env python3
import os
import signal
import sys
from pathlib import Path

if "/PID" not in sys.argv:
    sys.exit(1)
pid = int(sys.argv[sys.argv.index("/PID") + 1])


def children_of(parent: int) -> list[int]:
    found: list[int] = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text()
        except OSError:
            continue
        rparen = text.rfind(")")
        if rparen == -1:
            continue
        fields = text[rparen + 2 :].split()
        if len(fields) < 2:
            continue
        if int(fields[1]) == parent:
            found.append(int(entry.name))
    return found


def kill_tree(target: int) -> None:
    for child in children_of(target):
        kill_tree(child)
    try:
        os.kill(target, signal.SIGKILL)
    except ProcessLookupError:
        pass


kill_tree(pid)
sys.exit(0)
"""


@pytest.fixture
def windows_kill_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a taskkill shim and select the win32 kill / probe branch."""
    shim = tmp_path / "bin" / "taskkill"
    shim.parent.mkdir()
    shim.write_text(_SHIM, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.mark.skipif(sys.platform == "win32", reason="real Windows uses the OS taskkill")
@pytest.mark.skipif(not Path("/proc").is_dir(), reason="shim walks /proc for descendants")
class TestWindowsKillBranch:
    async def test_timeout_kills_the_python_grandchild(
        self, tmp_path: Path, windows_kill_branch: None
    ) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch(
            "c1",
            "shell",
            {"command": escape_probe_cmd(tmp_path), "timeout_secs": 1},
            session,
        )
        assert result.status == "success"
        if _KILL_REFUSED in result.output:
            pytest.fail(f"Windows kill path reported a refusal on the shim: {result.output}")
        assert "timed out after 1s — process group killed" in result.output
        await _assert_group_gone(tmp_path)
        (tmp_path / "release.txt").write_text("", encoding="utf-8")
        await asyncio.sleep(0.2)
        assert not (tmp_path / "kicked.txt").exists()

    async def test_cancel_kills_the_python_grandchild(
        self, tmp_path: Path, windows_kill_branch: None
    ) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        task = asyncio.create_task(
            dispatcher.dispatch("c1", "shell", {"command": escape_probe_cmd(tmp_path)}, session)
        )
        await _wait_for_file(tmp_path / "pgid.txt")
        await session.cancel()
        result = await task
        assert result.status == "success"
        if _KILL_REFUSED in result.output:
            pytest.fail(f"Windows kill path reported a refusal on the shim: {result.output}")
        assert "cancelled — process group killed" in result.output
        await _assert_group_gone(tmp_path)
        (tmp_path / "release.txt").write_text("", encoding="utf-8")
        await asyncio.sleep(0.2)
        assert not (tmp_path / "kicked.txt").exists()
