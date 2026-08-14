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
  themselves, so list them deliberately.  The allowlist is a policy rail,
  not a sandbox — real isolation for autonomous runs is the container
  (spec §12.5).

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
import os
import re
import shlex
import shutil
import signal
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

# Env var names that look like secrets.  Matched case-insensitively as a
# substring of the variable name, so OPENAI_API_KEY, GITHUB_TOKEN,
# AWS_SECRET_ACCESS_KEY, DB_PASSWORD, etc. are all dropped.
_SECRET_NAME_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSW|CREDENTIAL|KEYCHAIN)", re.IGNORECASE)

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
    ``LANG``, ...) pass through unchanged.
    """
    return {name: value for name, value in os.environ.items() if not _SECRET_NAME_RE.search(name)}


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


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the command's process group (POSIX) or process (Windows).

    POSIX: the child leads its own group (``start_new_session``), so
    SIGKILL to the group takes backgrounded grandchildren with it.
    Windows has no process-group kill: terminate the direct child.
    Grandchildren can escape until a Job Object is introduced (TD-1406).
    """
    if sys.platform == "win32":
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


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

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=session.workspace_path,
            env=sanitized_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
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
            if session.cancel_requested:
                header = "cancelled — process group killed"
            else:
                header = f"timed out after {timeout_secs}s — process group killed"
            _kill_process_group(proc)
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
        _kill_process_group(proc)
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
