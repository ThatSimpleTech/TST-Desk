"""Subprocess transports close before the loop does (TD-4835).

Cancellation during ``communicate()`` used to drop the child. The child
watcher then finalized the transport after the test loop was gone
(``BaseSubprocessTransport.__del__`` → ``Event loop is closed``). These
hooks fail if a site returns from cancellation without kill + wait.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from tstd import proc_lifecycle
from tstd.autonomy.charter import _git as charter_git
from tstd.autonomy.checkpoint import Checkpointer, checkpoint_start_error
from tstd.autonomy.revert import _git as revert_git
from tstd.autonomy.sandbox import inspect_runtime
from tstd.autonomy.supervisor import _git as supervisor_git
from tstd.keychain import LinuxSecretService
from tstd.memory_commit import MemoryCommitter
from tstd.proc_lifecycle import finish_subprocess, reap_subprocess, retain_subprocess_transport


class _DeadTransport:
    """A subprocess transport whose process has already exited."""

    def __init__(self) -> None:
        self.closed = False
        self.returncode: int | None = -9
        self._loop = object()
        self._proc: object | None = None

    def is_closing(self) -> bool:
        return self.closed

    def get_returncode(self) -> int | None:
        return self.returncode

    def close(self) -> None:
        self.closed = True


class _Hold:
    """A child whose ``communicate`` blocks until ``kill``."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.waited = False
        self.wait_calls = 0
        self.started = asyncio.Event()
        self._release = asyncio.Event()
        self.cancel_first_wait = False

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.started.set()
        await self._release.wait()
        return b"ok", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._release.set()

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.cancel_first_wait and self.wait_calls == 1:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            gate: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            return await gate
        self.waited = True
        return -9 if self.returncode is None else self.returncode


async def _drive(proc: _Hold, call: Awaitable[object]) -> None:
    task = asyncio.create_task(call)
    await proc.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed
    assert proc.waited


class TestFinishSubprocess:
    async def test_success_does_not_kill(self) -> None:
        proc = _Hold()
        proc._release.set()
        proc.returncode = 0
        out, err = await finish_subprocess(proc, timeout=5)  # type: ignore[arg-type]
        assert (out, err) == (b"ok", b"")
        assert proc.killed is False

    async def test_timeout_kills_and_reaps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _Hold()

        async def boom(
            awaitable: object,
            timeout: float | None = None,  # noqa: ASYNC109 - matches asyncio.wait_for
        ) -> object:
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            raise TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", boom)
        with pytest.raises(TimeoutError):
            await finish_subprocess(proc, timeout=30)  # type: ignore[arg-type]
        assert proc.killed
        assert proc.waited

    async def test_cancel_kills_and_reaps(self) -> None:
        proc = _Hold()
        await _drive(proc, finish_subprocess(proc, timeout=30))  # type: ignore[arg-type]

    async def test_cancelled_wait_is_retried(self) -> None:
        proc = _Hold()
        proc.cancel_first_wait = True
        await _drive(proc, finish_subprocess(proc, timeout=30))  # type: ignore[arg-type]
        assert proc.wait_calls == 2

    async def test_reap_timeout_closes_a_dead_transport(self) -> None:
        proc = _Hold()
        proc.returncode = -9
        transport = _DeadTransport()
        proc._transport = transport  # type: ignore[attr-defined]
        await reap_subprocess(proc, timeout=0, kill=False)  # type: ignore[arg-type]
        assert transport.closed
        assert proc.killed is False

    async def test_reap_leaves_a_live_leader(self) -> None:
        proc = _Hold()
        transport = _DeadTransport()
        transport.returncode = None
        proc._transport = transport  # type: ignore[attr-defined]
        await reap_subprocess(proc, timeout=0, kill=False)  # type: ignore[arg-type]
        assert transport.closed is False

    async def test_reap_closes_a_dead_transport_whose_loop_is_gone(self) -> None:
        """``_call_connection_lost`` clears ``_loop`` and leaves ``_closed`` false."""
        proc = _Hold()
        proc.returncode = -9
        transport = _DeadTransport()
        transport._loop = None
        proc._transport = transport  # type: ignore[attr-defined]
        await reap_subprocess(proc, timeout=0, kill=False)  # type: ignore[arg-type]
        assert transport.closed
        assert proc.killed is False


class _Parked:
    """A live child whose ``wait`` does not return until ``release``."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 4242
        self._gate = asyncio.Event()
        self._transport = _DeadTransport()
        self._transport.returncode = None

    async def wait(self) -> int:
        await self._gate.wait()
        return -9


class _Poll:
    """Stand-in for ``Popen.poll``. The real call is a ``waitpid``."""

    def __init__(self, code: int | None) -> None:
        self.code = code
        self.calls = 0

    def poll(self) -> int | None:
        self.calls += 1
        return self.code


class _Waitid:
    """Fake ``os.waitid``. ``kind`` is ``live``, ``exited``, or ``echild``."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls: list[tuple[int, int, int]] = []

    def __call__(self, idtype: int, pid: int, options: int) -> object | None:
        self.calls.append((idtype, pid, options))
        wexited = getattr(os, "WEXITED", 0)
        wnohang = getattr(os, "WNOHANG", 0)
        wnowait = getattr(os, "WNOWAIT", 0)
        if wexited and wnohang and wnowait:
            assert options & (wexited | wnohang | wnowait) == wexited | wnohang | wnowait
        p_pid = getattr(os, "P_PID", None)
        if isinstance(p_pid, int):
            assert idtype == p_pid
        if self.kind == "live":
            return None
        if self.kind == "exited":
            return object()
        raise ChildProcessError(10, "No child processes")


def _boom(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("os.waitid was called")


def _use_waitid(monkeypatch: pytest.MonkeyPatch, kind: str) -> _Waitid | None:
    """Force one waitid branch. A direct ``os.waitid`` call fails the test."""
    monkeypatch.setattr(os, "waitid", _boom, raising=False)
    script = None if kind == "absent" else _Waitid(kind)

    def platform() -> Callable[..., object] | None:
        return script

    monkeypatch.setattr(proc_lifecycle, "_platform_waitid", platform)
    return script


def _parked(code: int | None) -> tuple[_Parked, _Poll]:
    proc = _Parked()
    poll = _Poll(code)
    proc._transport._proc = poll
    return proc, poll


def _closer() -> asyncio.Task[None]:
    closers = [task for task in asyncio.all_tasks() if task.get_name() == "shell-transport-close"]
    assert len(closers) == 1
    return closers[0]


# No ``os.waitid`` (macOS 3.11), a live child, and ECHILD. All three stay open.
_LIVE_WAITID = ("absent", "live", "echild")


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("absent", False),
        ("live", False),
        ("echild", False),
        ("exited", True),
    ],
)
def test_exited_unreaped_branches(
    monkeypatch: pytest.MonkeyPatch, branch: str, expected: bool
) -> None:
    monkeypatch.setattr(os, "waitid", _boom, raising=False)
    script = None if branch == "absent" else _Waitid(branch)
    assert proc_lifecycle._exited_unreaped(4242, waitid=script) is expected
    if script is not None:
        assert [call[1] for call in script.calls] == [4242]


def test_non_positive_pid_does_not_call_waitid() -> None:
    script = _Waitid("exited")
    assert proc_lifecycle._exited_unreaped(0, waitid=script) is False
    assert proc_lifecycle._exited_unreaped(-1, waitid=script) is False
    assert script.calls == []


class TestRetainTransport:
    async def test_dead_transport_closes_without_a_watcher(self) -> None:
        proc = _Hold()
        proc.returncode = -9
        transport = _DeadTransport()
        transport._loop = None
        proc._transport = transport  # type: ignore[attr-defined]
        retain_subprocess_transport(proc)  # type: ignore[arg-type]
        assert transport.closed
        assert not any(task.get_name() == "shell-transport-close" for task in asyncio.all_tasks())

    @pytest.mark.parametrize("branch", _LIVE_WAITID)
    async def test_live_leader_is_not_closed(
        self, monkeypatch: pytest.MonkeyPatch, branch: str
    ) -> None:
        script = _use_waitid(monkeypatch, branch)
        proc, poll = _parked(None)
        retain_subprocess_transport(proc)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert proc._transport.closed is False
        assert poll.calls == 1
        if script is not None:
            assert [call[1] for call in script.calls] == [proc.pid]
        closer = _closer()
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert proc._transport.closed is False

    @pytest.mark.parametrize("branch", _LIVE_WAITID)
    async def test_cancelled_watcher_closes_after_the_child_exits(
        self, monkeypatch: pytest.MonkeyPatch, branch: str
    ) -> None:
        """Loop shutdown cancels the watcher. A returncode of -9 still closes."""
        script = _use_waitid(monkeypatch, branch)
        proc, poll = _parked(None)
        retain_subprocess_transport(proc)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        proc.returncode = -9
        proc._transport.returncode = -9
        closer = _closer()
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert proc._transport.closed
        assert poll.calls == 1
        if script is not None:
            assert [call[1] for call in script.calls] == [proc.pid]

    async def test_unreaped_exit_closes_without_a_returncode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``waitid`` status closes while the transport returncode is still None."""
        script = _use_waitid(monkeypatch, "exited")
        proc, poll = _parked(None)
        retain_subprocess_transport(proc)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert proc._transport.closed
        assert poll.calls == 0
        assert script is not None
        assert [call[1] for call in script.calls] == [proc.pid]
        assert not any(task.get_name() == "shell-transport-close" for task in asyncio.all_tasks())

    async def test_echild_defers_to_poll(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Already reaped: ECHILD, then ``Popen.poll`` carries the code."""
        script = _use_waitid(monkeypatch, "echild")
        proc, poll = _parked(-9)
        retain_subprocess_transport(proc)  # type: ignore[arg-type]
        assert proc._transport.closed
        assert poll.calls == 1
        assert script is not None and [call[1] for call in script.calls] == [proc.pid]


async def _checkpoint(workspace: Path) -> None:
    await Checkpointer(workspace, "sess")._git("status")


async def _gate(workspace: Path) -> None:
    await checkpoint_start_error(workspace)


async def _charter(workspace: Path) -> None:
    await charter_git(workspace, "status")


async def _revert(workspace: Path) -> None:
    await revert_git(workspace, "status")


async def _supervisor(workspace: Path) -> None:
    await supervisor_git(workspace, "status")


async def _memory(workspace: Path) -> None:
    await MemoryCommitter(workspace)._git("status")


async def _runtime(_workspace: Path) -> None:
    await inspect_runtime("podman")


async def _keychain(_workspace: Path) -> None:
    await LinuxSecretService().get_secret("tst-openrouter")


@pytest.mark.parametrize(
    "start",
    [
        _checkpoint,
        _gate,
        _charter,
        _revert,
        _supervisor,
        _memory,
        _runtime,
        _keychain,
    ],
)
async def test_cancelled_subprocess_is_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: Callable[[Path], Awaitable[None]],
) -> None:
    proc = _Hold()

    async def fake_exec(*_args: object, **_kwargs: object) -> _Hold:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await _drive(proc, start(tmp_path))
