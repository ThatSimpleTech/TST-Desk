"""Shell execution — run commands in the workspace (TD-605).

The shell tool runs a command through the system shell with the session's
workspace as the working directory.  Three safety rails surround it:

- **Environment sanitization.**  The child inherits a copy of the daemon's
  environment with secret-shaped names removed (``sanitized_env``).  API
  keys themselves live in the OS keychain and never in the daemon's
  environment by design (see ``keychain.py``); the filter covers the
  inherited-environment vector.
- **Process-group kill.**  The child starts a new session
  (``start_new_session``), so it leads its own process group.  On timeout
  or cancel the whole group is SIGKILLed — backgrounded children cannot
  outlive the command.  Windows has no process-group kill; the direct
  child is terminated instead.
- **``allowed_commands`` allowlist.**  When configured, each top-level
  segment's leading binary is resolved with ``shutil.which`` and matched
  by basename.  Unresolvable binaries and unparseable commands are
  refused fail-closed.  Only the leading binary of each segment is
  checked: wrappers such as ``sh -c``, ``sudo``, or ``env`` match as
  themselves, so list them deliberately — and know that listed binaries
  can re-exec others: ``find -exec``, ``xargs``, ``make``, and every
  interpreter walk straight through a basename match.  The allowlist is
  a policy rail, not a sandbox — real isolation for autonomous runs is
  the container (spec §12.5).

Output streams to the timeline as ``shell_output`` events while the
command runs, and the final result carries the exit code plus capped
stdout/stderr sections.  A non-zero exit is a normal result the model can
reason about, not an error.

Subprocess work uses the event loop's native subprocess support rather
than ``asyncio.to_thread``: streaming reads are already async (AGENTS.md
§6 reserves ``to_thread`` for blocking syscalls).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..protocol import ShellOutput
from ..session import Session

# Read chunk size for draining stdout/stderr.
_READ_CHUNK = 4096
# How long to wait for drains to settle after killing the process group.
_KILL_GRACE_SECS = 5.0
# Cap on how long the Windows taskkill helper may stall the event loop.
_TASKKILL_TIMEOUT_SECS = 2.0
# taskkill's "the process is not running" exit code.
_TASKKILL_NOT_FOUND = 128
# Windows-only creation flags, absent from `subprocess` on other platforms.
# 0 means "no extra flags", which is what Popen requires off Windows.
_CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_log = logging.getLogger(__name__)

# Env var names that look like secrets.  Matched case-insensitively as a
# substring of the variable name, so OPENAI_API_KEY, GITHUB_TOKEN,
# AWS_SECRET_ACCESS_KEY, DB_PASSWORD, MYSQL_PWD, etc. are all dropped.
# (PWD and OLDPWD go too — the child shell re-derives them from its cwd.)
_SECRET_NAME_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSW|PWD|CREDENTIAL|KEYCHAIN)", re.IGNORECASE)
# AUTH as a word, not a substring: DOCKER_AUTH_CONFIG and SOME_AUTH drop,
# but GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL must survive or the child cannot
# commit as the user.
_AUTH_NAME_RE = re.compile(r"(^|_)AUTH($|_)", re.IGNORECASE)
# SSH_AUTH_SOCK names a capability (the agent socket), not a secret value;
# dropping it would silently break every git-over-ssh command the user
# approves.  The filter's job is secret values, not sandboxing.
_AUTH_NAME_EXEMPT = frozenset({"SSH_AUTH_SOCK"})
# A URL with embedded credentials (user:pass@ before the host) is a secret
# regardless of the variable's name — DATABASE_URL, REDIS_URL, SMTP URLs.
_CREDENTIAL_URL_VALUE_RE = re.compile(r"://[^/\s:]+:[^/\s@]+@")

# Shell operators that separate top-level commands.  Newline is a command
# separator too.
_SEGMENT_SPLIT_RE = re.compile(r"[|&;()<>\n]+")
# Leading VAR=value assignments before a segment's binary.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class ShellPolicy:
    """Policy knobs for the shell tool.

    Attributes:
        allowed_commands: When not None, only these binaries may run
            (matched on the basename of the ``which``-resolved path).
            None means unrestricted.
        max_stream_chars: Per-stream capture cap.  Sized so stdout +
            stderr + headers stay under the dispatcher's result cap.
    """

    allowed_commands: tuple[str, ...] | None = None
    max_stream_chars: int = 20_000


def sanitized_env() -> dict[str, str]:
    """The daemon environment with secret-shaped variables removed.

    API keys, tokens, passwords, and keychain material are never passed
    to child processes (TD-605).  Ordinary variables (``PATH``, ``HOME``,
    ``LANG``, ...) pass through unchanged.  TD-4805 widened the net:
    ``*_AUTH`` word forms (``DOCKER_AUTH_CONFIG``), ``PWD`` carriers
    (``MYSQL_PWD``), and any variable whose *value* embeds credentials in
    a URL (user:password@ before the host), regardless of name.
    """
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        if _SECRET_NAME_RE.search(name):
            continue
        if name not in _AUTH_NAME_EXEMPT and _AUTH_NAME_RE.search(name):
            continue
        if _CREDENTIAL_URL_VALUE_RE.search(value):
            continue
        env[name] = value
    return env


def _binaries(command: str) -> list[str]:
    """The leading binary of each top-level command segment.

    Raises ``ValueError`` on anything that cannot be safely parsed —
    fail closed, since an unparseable command cannot be checked against
    the allowlist.
    """
    if "`" in command:
        raise ValueError("backtick substitution is not allowed when an allowlist is configured")
    binaries: list[str] = []
    for segment in _SEGMENT_SPLIT_RE.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError as e:
            raise ValueError(f"cannot parse command segment {segment!r}: {e}") from e
        for token in tokens:
            if _ASSIGNMENT_RE.match(token):
                continue  # env assignment prefix, not the binary
            binaries.append(token)
            break
    return binaries


def _resolved_name(resolved: str) -> str:
    """The allowlist-matching name for a resolved binary path.

    POSIX matches the basename.  On Windows, ``shutil.which`` resolves
    through PATHEXT (``echo`` → ``echo.EXE``) and the filesystem is
    case-insensitive, so the name is lowercased and the executable
    extension stripped before matching.
    """
    name = Path(resolved).name
    if sys.platform == "win32":
        name = Path(name).stem.lower()
    return name


def check_allowed(command: str, policy: ShellPolicy) -> None:
    """Enforce the allowlist on *command*.

    Resolves each segment's binary with ``shutil.which`` and matches the
    basename of the resolved path, so ``git`` and ``/usr/bin/git`` both
    match ``git``.  Unresolvable binaries are refused fail-closed.

    Raises:
        ValueError: When any binary is not allowed (or cannot be resolved).
    """
    if policy.allowed_commands is None:
        return
    allowed = set(policy.allowed_commands)
    if sys.platform == "win32":
        # Same normalization _resolved_name applies to resolved paths.
        allowed = {Path(name).stem.lower() for name in allowed}
    for binary in _binaries(command):
        resolved = shutil.which(binary)
        if resolved is None:
            raise ValueError(f"cannot resolve binary {binary!r}; refusing to run unverified")
        name = _resolved_name(resolved)
        if name not in allowed:
            raise ValueError(
                f"{binary!r} (resolved to {name!r}) is not in allowed_commands {sorted(allowed)}"
            )


@dataclass
class _StreamCapture:
    """Bounded capture buffer for one output stream."""

    cap: int
    parts: list[str] = field(default_factory=list)
    total: int = 0
    truncated: bool = False

    def offer(self, text: str) -> str | None:
        """Accept *text*; return the slice to keep/emit, or None past cap.

        Reading continues past the cap (the pipe must drain or the child
        blocks); only buffering and event emission stop.
        """
        if self.truncated:
            return None
        room = self.cap - self.total
        if len(text) >= room:
            self.truncated = True
            if room <= 0:
                return None
            self.total = self.cap
            return text[:room]
        self.total += len(text)
        return text

    @property
    def text(self) -> str:
        return "".join(self.parts)


def _kill_windows_tree(pid: int) -> str | None:
    """Kill *pid* and its descendants with ``taskkill /T /F`` (TD-1406).

    ``Process.kill`` maps to ``TerminateProcess``, which kills one process
    and leaves its children running — the shell dies and its backgrounded
    grandchildren keep the pipes open.  ``taskkill /T`` walks the parent
    chain the OS already records and kills the tree, which is the closest
    Windows equivalent of ``killpg``.  It ships with Windows, so this
    stays a stdlib-only change.

    ``CREATE_NEW_PROCESS_GROUP`` at the spawn is what makes the tree
    unambiguous: the child is a group leader, so nothing above it is in
    scope for the walk.

    This blocks the event loop, which §6 otherwise forbids: a deliberate,
    bounded exception on the two cancellation paths only: the kill must
    have landed before the handler re-raises, and awaiting there can be
    cancelled again, so the kill would never land.  The timeout path is
    normal async flow and offloads instead — see
    ``_kill_process_group_offloaded``.  ``taskkill`` normally returns in
    tens of milliseconds; the timeout is the cap on how long a Windows
    cancel can stall the loop.  Nothing blocks on POSIX, where the kill
    is a syscall.

    ``creationflags`` keeps the helper's own console window hidden.
    """
    try:
        completed = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT_SECS,
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    except FileNotFoundError:  # pragma: no cover — taskkill ships with Windows
        return "taskkill not found; only the direct child was terminated"
    except subprocess.TimeoutExpired:
        return "taskkill timed out; the process tree may still be running"
    if completed.returncode == _TASKKILL_NOT_FOUND:
        return None  # already gone
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        return f"process tree kill refused by the OS ({detail or completed.returncode})"
    return None


def _kill_process_group(proc: asyncio.subprocess.Process) -> str | None:
    """Kill the command's whole process tree.

    POSIX: the child leads its own group (``start_new_session``), so
    SIGKILL to the group takes backgrounded grandchildren with it.
    Windows has no ``killpg``; the child leads its own process group
    (``CREATE_NEW_PROCESS_GROUP``) and ``taskkill /T /F`` walks the tree
    from it.  ``proc.kill()`` still runs first there, so the direct child
    dies even if the helper cannot (TD-1406).

    Returns None when the kill was delivered (or the processes were
    already gone).  macOS occasionally vetoes same-uid kills with EPERM —
    the refusal attaches to the process group, no userspace retry or
    external ``kill`` breaks it, and the command then runs to completion —
    so a short note comes back for honest reporting instead.
    """
    if sys.platform == "win32":
        try:
            proc.kill()
        except ProcessLookupError:
            return None
        except OSError:
            return "process kill refused by the OS; it may still be running"
        return _kill_windows_tree(proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return None  # already gone
    except PermissionError:
        return "group kill refused by the OS (EPERM); processes may still be running"
    return None


async def _kill_process_group_offloaded(proc: asyncio.subprocess.Process) -> str | None:
    """``_kill_process_group`` without blocking the event loop (TD-1406).

    For callers in normal async flow — the timeout path, which is also the
    common one.  A cancellation handler must NOT use this: awaiting while a
    ``CancelledError`` is in flight can be cancelled again, and then the kill
    never lands.  Those two sites keep the blocking call deliberately.

    POSIX stays direct: ``killpg`` is a syscall, and a thread hop would cost
    more than it saves.
    """
    if sys.platform != "win32":
        return _kill_process_group(proc)
    return await asyncio.to_thread(_kill_process_group, proc)


def _check_workspace(workspace_path: str) -> None:
    """Refuse to run when the workspace directory is missing."""
    if not Path(workspace_path).is_dir():
        raise ValueError(f"workspace directory does not exist: {workspace_path}")


async def _drain(
    stream: asyncio.StreamReader,
    stream_name: Literal["stdout", "stderr"],
    session: Session,
    tool_call_id: str,
    capture: _StreamCapture,
) -> None:
    """Read *stream* to EOF, streaming chunks to the timeline.

    Emits a ``shell_output`` event per chunk while the capture has room,
    then keeps draining silently so the child never blocks on a full pipe.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        raw = await stream.read(_READ_CHUNK)
        if not raw:
            tail = decoder.decode(b"", final=True)
            if tail:
                kept = capture.offer(tail)
                if kept is not None:
                    capture.parts.append(kept)
            return
        text = decoder.decode(raw)
        kept = capture.offer(text)
        if kept is None:
            continue
        capture.parts.append(kept)
        await session.event_log.add(
            ShellOutput(
                session_id=session.id,
                tool_call_id=tool_call_id,
                stream=stream_name,
                chunk=kept,
                seq=1,  # overwritten by the event log
            )
        )


def _format_result(
    header: str,
    exit_code: int,
    out: _StreamCapture,
    err: _StreamCapture,
) -> str:
    """Build the model-facing result string."""
    lines: list[str] = []
    if header:
        lines.append(header)
    lines.append(f"exit code: {exit_code}")
    for label, capture in (("stdout", out), ("stderr", err)):
        lines.append(f"── {label} ──")
        text = capture.text
        if capture.truncated:
            text += "\n… [truncated: stream output exceeds cap]"
        lines.append(text or "(empty)")
    return "\n".join(lines)


async def run_shell(
    session: Session | None,
    command: str,
    timeout_secs: int = 30,
    tool_call_id: str = "",
    *,
    policy: ShellPolicy | None = None,
) -> str:
    """Run *command* in the session's workspace and return its result.

    Streams stdout/stderr to the timeline while the command runs, kills
    the process group on timeout or cancel, and returns the exit code with
    capped output.  Non-zero exits are normal results, not errors.

    Raises:
        ValueError: Refusals — no session, invalid timeout, allowlist
            rejection, missing workspace, or a spawn failure.
    """
    if session is None:
        raise ValueError("shell requires an active session")
    if timeout_secs < 1:
        raise ValueError("timeout_secs must be a positive integer")
    pol = policy or ShellPolicy()
    check_allowed(command, pol)
    _check_workspace(session.workspace_path)
    if session.cancel_requested:
        return "cancelled — command not started"

    # Spawn under a shield so a cancellation landing mid-spawn does not
    # cancel the spawn coroutine itself — the OS child may already exist,
    # and losing the handle would orphan the whole process group (TD-605
    # AC2; the TestCancel flake family).  On cancellation, wait for the
    # spawn to settle, kill the group, and let cancellation propagate.
    spawn = asyncio.ensure_future(
        asyncio.create_subprocess_shell(
            command,
            cwd=session.workspace_path,
            env=sanitized_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # POSIX: setsid, so the child leads a killable process group.
            # start_new_session is silently ignored on Windows, where the
            # equivalent is a creation flag — without it the child joins
            # the daemon's own group and a tree walk from its pid has no
            # defined edge (TD-1406).
            start_new_session=True,
            creationflags=_CREATE_NEW_PROCESS_GROUP,
        )
    )
    try:
        proc = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        with contextlib.suppress(OSError, asyncio.CancelledError):
            proc = await spawn
            kill_note = _kill_process_group(proc)
            if kill_note:
                _log.warning("cancelled mid-spawn: %s (pid %s)", kill_note, proc.pid)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECS)
        raise
    except OSError as e:
        raise ValueError(f"could not start shell: {e}") from e

    stdout_stream = proc.stdout
    stderr_stream = proc.stderr
    if stdout_stream is None or stderr_stream is None:  # pragma: no cover — PIPE guarantees both
        raise ValueError("shell pipes were not created")

    out = _StreamCapture(cap=pol.max_stream_chars)
    err = _StreamCapture(cap=pol.max_stream_chars)
    work_task: asyncio.Task[None] | None = None
    cancel_task: asyncio.Task[None] | None = None
    header = ""
    try:

        async def _work() -> None:
            # Drains live inside the work task so cancelling it cancels
            # them too — no orphaned readers.
            await asyncio.gather(
                _drain(stdout_stream, "stdout", session, tool_call_id, out),
                _drain(stderr_stream, "stderr", session, tool_call_id, err),
                proc.wait(),
            )

        work_task = asyncio.create_task(_work())
        cancel_task = asyncio.create_task(session.wait_for_cancel())
        await asyncio.wait(
            {work_task, cancel_task},
            timeout=float(timeout_secs),
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not work_task.done():
            # Not a cancellation handler — this is the timeout and the
            # cooperative-cancel path, so the Windows tree kill goes to a
            # thread rather than stalling the socket for up to 2s (§6).
            kill_note = await _kill_process_group_offloaded(proc)
            detail = kill_note or "process group killed"
            if session.cancel_requested:
                header = f"cancelled — {detail}"
            else:
                header = f"timed out after {timeout_secs}s — {detail}"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(work_task, timeout=_KILL_GRACE_SECS)
            # Reap the direct child even if drains are stuck on a pipe
            # held open by an escaped grandchild.
            if proc.returncode is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECS)
    except asyncio.CancelledError:
        # SessionRunner.cancel() cancels the loop task; the group must
        # die with it.
        kill_note = _kill_process_group(proc)
        if kill_note:
            _log.warning("cancelled: %s (pid %s)", kill_note, proc.pid)
        raise
    finally:
        for task in (work_task, cancel_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # A cancelled process may have no returncode; -9 is the conventional
    # "killed by SIGKILL" sentinel (SIGKILL itself is absent on Windows).
    exit_code = proc.returncode if proc.returncode is not None else -9
    return _format_result(header, exit_code, out, err)
