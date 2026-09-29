"""Subprocess transports close before the loop does (TD-4835).

Cancellation during ``communicate()`` used to drop the child. The child
watcher then finalized the transport after the test loop was gone
(``BaseSubprocessTransport.__del__`` → ``Event loop is closed``). These
hooks fail if a site returns from cancellation without kill + wait.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from tstd.autonomy.charter import _git as charter_git
from tstd.autonomy.checkpoint import Checkpointer, checkpoint_start_error
from tstd.autonomy.revert import _git as revert_git
from tstd.autonomy.sandbox import inspect_runtime
from tstd.autonomy.supervisor import _git as supervisor_git
from tstd.keychain import LinuxSecretService
from tstd.memory_commit import MemoryCommitter
from tstd.proc_lifecycle import finish_subprocess, reap_subprocess


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
