"""Reap asyncio subprocesses before the event loop can close.

A cancelled ``communicate()`` leaves the child running. The child watcher
holds the transport until that child exits; if the loop is already closed
by then, ``BaseSubprocessTransport.__del__`` calls ``call_soon`` and raises
``RuntimeError: Event loop is closed`` (TD-4835). Kill and ``wait`` while
the loop is still open, including from inside a cancellation handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

# A child that survives SIGKILL must not pin the daemon. The shell tool
# already bounds its post-kill wait at five seconds; git and sandbox
# children share that grace. Keychain passes a shorter one (TD-1105).
_DEFAULT_REAP_SECS = 5.0


async def finish_subprocess(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,  # noqa: ASYNC109 - communicate bound; the reap uses asyncio.timeout
    stdin: bytes | None = None,
    reap_timeout: float = _DEFAULT_REAP_SECS,
) -> tuple[bytes, bytes]:
    """``communicate`` with *timeout*. On timeout or cancellation, reap first.

    ``TimeoutError`` and ``CancelledError`` propagate only after the
    transport has been closed, or after *reap_timeout* has elapsed.
    """
    try:
        return await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        await reap_subprocess(proc, timeout=reap_timeout)
        raise


def kill_left_leader_running(
    proc: asyncio.subprocess.Process,
    kill_note: str | None,
) -> bool:
    """True when a group-kill refusal left the leader alive.

    ``reap_subprocess`` SIGKILLs whatever is still running. Doing that
    after a refused ``killpg`` orphans grandchildren that still hold the
    pipes, and the transport is then finalized in ``__del__`` (TD-4835).
    A leader that has already exited (the Windows tree-kill can report a
    note and still have terminated the direct child) is not "left running".
    """
    if kill_note is None or proc.returncode is not None:
        return False
    transport = getattr(proc, "_transport", None)
    popen = getattr(transport, "_proc", None) if transport is not None else None
    return popen is None or popen.poll() is None


async def reap_subprocess(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,  # noqa: ASYNC109 - exit-wait deadline, retried with asyncio.timeout
    kill: bool = True,
) -> None:
    """Kill a still-running child and wait until its transport can close.

    A further ``cancel()`` while waiting cancels the exit waiter. That
    attempt is retried until *timeout* so the transport is not left for
    ``__del__``. If a cancellation was absorbed to finish the wait, it is
    re-raised afterwards — a timeout must not swallow a real cancel.

    *kill* is false when the caller already decided the leader must stay
    (a refused group kill). The wait still runs so a leader that dies on
    its own is reaped; a leader that is still alive is not closed, because
    ``transport.close()`` would SIGKILL it.
    """
    if kill and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    cancelled = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            # ``asyncio.timeout`` stays on this task. ``wait_for`` would
            # wrap ``wait()`` in a child task whose cancellation is a
            # different waiter, and the caller's task would not be the
            # one blocked in ``wait``.
            async with asyncio.timeout(remaining):
                await proc.wait()
        except TimeoutError:
            break
        except asyncio.CancelledError:
            cancelled = True
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            continue
        else:
            break
    # ``wait`` returns only once every pipe disconnects. A grandchild that
    # outlives the leader keeps the pipe open, the grace elapses, and the
    # transport would otherwise be destroyed after the loop is gone.
    await _release_dead_transport(proc)
    if cancelled:
        raise asyncio.CancelledError


# Tasks that close a transport after the tool has returned. A function-scoped
# test loop stops before ThreadedChildWatcher's extra ``call_soon`` (3.11),
# and ``__del__`` then warns. The set keeps the task, and the task keeps the
# process, across that collection (TD-4851).
_RETAINED: set[asyncio.Task[None]] = set()


def retain_subprocess_transport(proc: asyncio.subprocess.Process | None) -> None:
    """Close a dead child's transport now, or once it exits.

    Called from ``run_shell``'s ``finally``, including a cancellation
    handler. It does not await. The next ``await`` in a cancelled task
    raises ``CancelledError`` and would skip the close (TD-4851).

    A refused group kill leaves the leader alive. ``transport.close()``
    SIGKILLs a live child, which the refusal tests forbid and which
    orphans grandchildren that still hold the pipes (TD-4835). A watcher
    closes after the exit, including when loop shutdown cancels it.
    """
    if proc is None:
        return
    transport = getattr(proc, "_transport", None)
    if transport is None or _is_closing(transport):
        return
    if _child_is_dead(proc, transport):
        _close_transport(transport)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_close_when_exited(proc), name="shell-transport-close")
    _RETAINED.add(task)
    # Cancel-before-start throws into a coroutine that has not reached
    # ``wait``. The body never runs. The callback still closes a child
    # that has exited, which is what loop shutdown does to a watcher
    # that has not been scheduled yet.
    task.add_done_callback(lambda done: _retain_done(done, proc))


def _retain_done(task: asyncio.Task[None], proc: asyncio.subprocess.Process) -> None:
    _RETAINED.discard(task)
    transport = getattr(proc, "_transport", None)
    if transport is None or _is_closing(transport):
        return
    if _child_is_dead(proc, transport):
        _close_transport(transport)


async def _close_when_exited(proc: asyncio.subprocess.Process) -> None:
    cancelled = False
    try:
        if proc.returncode is None:
            await proc.wait()
    except asyncio.CancelledError:
        # Runner teardown cancels this task while the loop is still open
        # (``asyncio.runners._cancel_all_tasks``). The child has usually
        # been SIGKILLed already. Clear the cancel so the close below can
        # yield once for the pipe callbacks, then restore it.
        cancelled = True
        _clear_cancellation()
    await _release_dead_transport(proc)
    if cancelled:
        raise asyncio.CancelledError


def _clear_cancellation() -> None:
    task = asyncio.current_task()
    if task is None:
        return
    while task.cancelling():
        task.uncancel()


async def _release_dead_transport(proc: asyncio.subprocess.Process) -> None:
    """Close a transport whose process has already exited.

    Pipe ``close`` schedules its callbacks. They have to run while this
    loop is still open. A dead child whose ``_loop`` was cleared by
    ``_call_connection_lost`` is still closed: that path leaves
    ``_closed`` false, which is what ``__del__`` warns on (TD-4851).
    """
    transport = getattr(proc, "_transport", None)
    if transport is None or _is_closing(transport):
        return
    if not _child_is_dead(proc, transport):
        return
    _close_transport(transport)
    await asyncio.sleep(0)


def _is_closing(transport: Any) -> bool:
    is_closing = getattr(transport, "is_closing", None)
    if not callable(is_closing):
        return False
    return bool(is_closing())


def _close_transport(transport: Any) -> None:
    close = getattr(transport, "close", None)
    if not callable(close):
        return
    try:
        close()
    except RuntimeError:
        # ``close`` sets ``_closed`` before it touches the pipes. A loop
        # that is already gone raises from the pipe half. ``__del__``
        # warns only when ``_closed`` is still false.
        transport._closed = True


def _child_is_dead(proc: asyncio.subprocess.Process, transport: Any) -> bool:
    """True when closing the transport will not SIGKILL a live leader.

    ``BaseSubprocessTransport.close`` kills when the transport returncode
    is still ``None`` and ``Popen.poll`` is ``None``. A missing ``Popen``
    with no returncode is treated as live: that is the refused-kill
    shape, and a guess would orphan the grandchildren.
    """
    if _transport_returncode(transport) is not None or proc.returncode is not None:
        return True
    if _exited_unreaped(_pid(proc, transport)):
        return True
    popen = getattr(transport, "_proc", None)
    if popen is None:
        return False
    poll = getattr(popen, "poll", None)
    if not callable(poll):
        return False
    try:
        return poll() is not None
    except OSError:
        return False


def _transport_returncode(transport: Any) -> int | None:
    getter = getattr(transport, "get_returncode", None)
    if not callable(getter):
        return None
    code = getter()
    if isinstance(code, int):
        return code
    return None


def _pid(proc: asyncio.subprocess.Process, transport: Any) -> int:
    getter = getattr(transport, "get_pid", None)
    if callable(getter):
        pid = getter()
        if isinstance(pid, int):
            return pid
    pid = getattr(proc, "pid", 0)
    return pid if isinstance(pid, int) else 0


def _exited_unreaped(pid: int) -> bool:
    """True when *pid* has exited and this process has not collected it.

    ``waitid`` with ``WNOWAIT`` does not reap, so ``ThreadedChildWatcher``
    can still ``waitpid`` the same child. CPython 3.11 on Linux has
    ``os.waitid``. The macOS builds of 3.11 and 3.12 do not; those fall
    through to the transport returncode and ``Popen.poll``.
    """
    waitid = getattr(os, "waitid", None)
    p_pid = getattr(os, "P_PID", None)
    wexited = getattr(os, "WEXITED", None)
    wnohang = getattr(os, "WNOHANG", None)
    wnowait = getattr(os, "WNOWAIT", None)
    if (
        not callable(waitid)
        or p_pid is None
        or wexited is None
        or wnohang is None
        or wnowait is None
        or pid <= 0
    ):
        return False
    try:
        info = waitid(p_pid, pid, wexited | wnohang | wnowait)
    except ChildProcessError:
        return True
    except OSError:
        return False
    return info is not None
