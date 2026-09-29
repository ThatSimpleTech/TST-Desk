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


async def _release_dead_transport(proc: asyncio.subprocess.Process) -> None:
    """Close a transport whose process has already exited.

    Pipe ``close`` schedules its callbacks. They have to run while this
    loop is still open.
    """
    transport = getattr(proc, "_transport", None)
    if transport is None or transport.is_closing():
        return
    if getattr(transport, "_loop", None) is None:
        return
    popen = getattr(transport, "_proc", None)
    still_running = transport.get_returncode() is None and (popen is None or popen.poll() is None)
    if still_running:
        return
    transport.close()
    await asyncio.sleep(0)
