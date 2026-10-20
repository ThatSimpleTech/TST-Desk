"""Memory cap on shell commands (TD-5116).

The shell tool used to bound a command only in time: a runaway build could
take the box's memory, and the kernel's out-of-memory killer could take the
daemon with it. Every command now runs under ``caps.shell_memory_mb`` from
the workspace config, half of RAM when unset:

- **systemd user scope** (Linux, where ``systemd-run --user --scope`` works
  and the user manager has the memory controller). Each command gets its
  own transient scope with ``MemoryMax``, ``MemorySwapMax=0`` (so a hog is
  killed at the cap instead of filling swap) and ``OOMPolicy=stop``. The
  kernel kills inside that scope only. Afterwards the unit's ``Result`` (or
  the scope's own ``memory.events``, when systemd has not caught up yet)
  says whether it did, so the result can say so.
- **``RLIMIT_AS``** (Linux without user scopes). The shell sets
  ``ulimit -v`` and then execs the command, so the child and everything it
  starts inherit the limit and cannot raise it. The kernel kills nothing:
  an allocation past the cap fails, and the command dies of its own
  out-of-memory error. The result reads that error.
- **Container** (autonomous sessions). Podman gets ``--memory``. A SIGKILL
  exit (137) under it is reported as the likely cause.

macOS does not enforce ``RLIMIT_AS`` and Windows has neither mechanism, so
the host shell runs uncapped there; the container cap still applies.

The host probe runs once per daemon, with the same scope properties a
command gets, and its answer holds until the daemon restarts (as Claude
Code holds its cgroup setup until relaunch).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import sys
import uuid
from dataclasses import dataclass
from typing import Literal

from ..host_memory import MIB
from ..proc_lifecycle import finish_subprocess
from ..session import Session

_log = logging.getLogger(__name__)

CapMethod = Literal["scope", "rlimit", "container"]

_PROBE_MB = 256
_PROBE_TIMEOUT_SECS = 5.0
_QUERY_TIMEOUT_SECS = 5.0
# After the kernel's count shows a kill, how long to give systemd to mark
# the unit failed, so it can be cleared rather than left in ``--failed``.
_SETTLE_POLLS = 20
_SETTLE_SECS = 0.05
# The exit status of a process killed by SIGKILL, as a shell reports it.
_SIGKILL_EXIT = 137

# Prints a marker, then the scope's own ``memory.max``. The marker needs
# ``$$`` and ``${v}`` to reach the shell as written: systemd 254+ expands
# both in ``systemd-run`` arguments unless told not to, which would rewrite
# every command that uses them. Builtins and ``cat`` only, so the probe
# does not depend on what else is installed.
_PROBE_MARKER = "verbatim"
_PROBE_SCRIPT = (
    f"v={_PROBE_MARKER}; cg=; "
    "while IFS= read -r line; do case $line in 0::*) cg=${line#0::};; esac; "
    'done < /proc/self/cgroup; [ "$$" -gt 1 ] && echo "${v}"; '
    'cat "/sys/fs/cgroup${cg}/memory.max"'
)

# Out-of-memory messages from the runtimes a shell command usually runs,
# matched case-insensitively on stderr. ENOMEM's own text covers C, shells
# and most tools.
_OOM_SIGNATURES = (
    "cannot allocate memory",
    "out of memory",
    "memoryerror",
    "std::bad_alloc",
    "memory allocation of",
    "native memory allocation",
)
# Out-of-memory messages about a runtime's own heap limit, which a command
# hits whatever the cap is. Removed before matching so they do not read as
# the cap.
_OWN_HEAP_LIMITS = ("javascript heap out of memory",)


@dataclass(frozen=True)
class _ScopeTools:
    """systemd's binaries, and whether ``systemd-run`` takes
    ``--expand-environment=no`` (254+; older releases never expand)."""

    systemd_run: str
    systemctl: str
    no_expand: bool


@dataclass
class _Probe:
    done: bool = False
    tools: _ScopeTools | None = None


_probe = _Probe()


@dataclass(frozen=True)
class ShellMemoryCap:
    """The cap one command runs under, and how it is applied.

    Attributes:
        mb: The cap in MiB, as the result reports it.
        method: ``scope``, ``rlimit`` or ``container``.
        unit: The scope's unit name (``scope`` only).
        limit_kib: The ``ulimit -v`` value (``rlimit`` only).
        tools: systemd's binaries, as the probe found them (``scope`` only).
    """

    mb: int
    method: CapMethod
    unit: str = ""
    limit_kib: int = 0
    tools: _ScopeTools | None = None


def _is_linux() -> bool:
    return sys.platform == "linux"


def reset_scope_probe() -> None:
    """Forget the probe's answer, so the next command probes again."""
    _probe.done = False
    _probe.tools = None


def _scope_args(unit: str, mb: int, *, no_expand: bool) -> list[str]:
    return [
        "--user",
        "--scope",
        "--quiet",
        *(["--expand-environment=no"] if no_expand else []),
        f"--unit={unit}",
        f"--property=MemoryMax={mb}M",
        "--property=MemorySwapMax=0",
        "--property=OOMPolicy=stop",
        "--",
    ]


def _unit_name(kind: str) -> str:
    return f"tstd-{kind}-{uuid.uuid4().hex[:16]}.scope"


async def _probe_once(systemd_run: str, env: dict[str, str], *, no_expand: bool) -> str | None:
    """Run the probe in a scope; None when it passed, else why it failed."""
    args = _scope_args(_unit_name("probe"), _PROBE_MB, no_expand=no_expand)
    try:
        proc = await asyncio.create_subprocess_exec(
            systemd_run,
            *args,
            "/bin/sh",
            "-c",
            _PROBE_SCRIPT,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await finish_subprocess(proc, timeout=_PROBE_TIMEOUT_SECS)
    except (OSError, TimeoutError) as e:
        return str(e) or type(e).__name__
    if out.decode(errors="replace").split() == [_PROBE_MARKER, str(_PROBE_MB * MIB)]:
        return None
    return err.decode(errors="replace").strip() or f"exit {proc.returncode}, unexpected output"


async def _probe_scope_tools(env: dict[str, str]) -> _ScopeTools | None:
    """systemd's tools when a user scope really enforces ``MemoryMax``.

    The probe starts a scope with a command's own properties, checks that
    the command text arrived unexpanded, and reads ``memory.max`` back.
    ``--expand-environment=no`` is tried first, then (for systemd before
    254, which has no such flag and never expands) plain. A session with no
    user manager, a user manager without the memory controller, and a
    systemd that refuses a property all fail, and commands fall back to
    ``RLIMIT_AS``.
    """
    # The daemon's PATH: the command's environment keeps it unchanged.
    systemd_run = shutil.which("systemd-run")
    systemctl = shutil.which("systemctl")
    if systemd_run is None or systemctl is None:
        _log.info("shell memory cap: no systemd-run on PATH; using RLIMIT_AS")
        return None
    why = ""
    for no_expand in (True, False):
        failure = await _probe_once(systemd_run, env, no_expand=no_expand)
        if failure is None:
            return _ScopeTools(systemd_run, systemctl, no_expand)
        why = failure
    _log.info("shell memory cap: systemd user scope unusable (%s); using RLIMIT_AS", why)
    return None


async def _scope_tools(env: dict[str, str]) -> _ScopeTools | None:
    if not _probe.done:
        tools = await _probe_scope_tools(env)
        _probe.tools = tools
        _probe.done = True
    return _probe.tools


def _rlimit_kib(mb: int) -> int:
    """``ulimit -v`` for *mb*, never above a limit the daemon already has.

    ``ulimit -v`` sets the soft and hard limits together. Asking for more
    than the inherited hard limit fails, and going above an inherited soft
    limit would loosen what the user set, so the lower one wins.
    """
    import resource

    want = mb * MIB
    for limit in resource.getrlimit(resource.RLIMIT_AS):
        if limit != resource.RLIM_INFINITY and 0 < limit < want:
            want = limit
    return max(1, want // 1024)


async def plan_cap(session: Session, env: dict[str, str]) -> ShellMemoryCap | None:
    """The cap *session*'s next command runs under, or None when there is none."""
    mb = session.boundary_config.caps.shell_memory_cap()
    if mb is None:
        return None
    if session.autonomy:
        return ShellMemoryCap(mb=mb, method="container")
    if not _is_linux():
        return None
    tools = await _scope_tools(env)
    if tools is not None:
        return ShellMemoryCap(mb=mb, method="scope", unit=_unit_name("shell"), tools=tools)
    kib = _rlimit_kib(mb)
    return ShellMemoryCap(mb=min(mb, kib // 1024) or 1, method="rlimit", limit_kib=kib)


def host_command(cap: ShellMemoryCap | None, command: str) -> str:
    """*command* as the host shell runs it under *cap*.

    Both wrappers ``exec`` into ``/bin/sh -c <command>``, so the spawned
    pid stays the process-group leader the timeout and cancel kills reach,
    and the command sees the same shell it always did.
    """
    if cap is None:
        return command
    if cap.method == "scope" and cap.tools is not None:
        args = _scope_args(cap.unit, cap.mb, no_expand=cap.tools.no_expand)
        argv = [cap.tools.systemd_run, *args, "/bin/sh", "-c", command]
        return "exec " + shlex.join(argv)
    if cap.method == "rlimit":
        return f"ulimit -v {cap.limit_kib} || exit 126; exec /bin/sh -c {shlex.quote(command)}"
    return command


def ran_out_of_memory(stderr: str) -> bool:
    """True when *stderr* carries an out-of-memory error the cap explains."""
    text = stderr.casefold()
    for phrase in _OWN_HEAP_LIMITS:
        text = text.replace(phrase, "")
    return any(sig in text for sig in _OOM_SIGNATURES)


async def _systemctl(tools: _ScopeTools, *args: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            tools.systemctl,
            "--user",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await finish_subprocess(proc, timeout=_QUERY_TIMEOUT_SECS)
    except (OSError, TimeoutError) as e:
        _log.warning("shell memory cap: systemctl %s failed: %s", args[0], e)
        return None
    return out.decode(errors="replace")


async def _unit_state(cap: ShellMemoryCap, tools: _ScopeTools) -> dict[str, str]:
    """``Result``, ``ActiveState`` and ``ControlGroup`` of the command's scope.

    A scope that ended cleanly has been collected already, which reads as
    ``Result=success`` with no control group.
    """
    out = await _systemctl(
        tools,
        "show",
        "--property=Result",
        "--property=ActiveState",
        "--property=ControlGroup",
        cap.unit,
    )
    state: dict[str, str] = {}
    for line in (out or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            state[key] = value.strip()
    return state


def _cgroup_oom_kills(cgroup: str) -> int:
    """The kernel's ``oom_kill`` count for *cgroup*, 0 when unreadable."""
    if not cgroup:
        return 0
    try:
        with open(f"/sys/fs/cgroup{cgroup}/memory.events", encoding="ascii") as f:
            for line in f:
                name, _, count = line.partition(" ")
                if name == "oom_kill":
                    return int(count)
    except (OSError, ValueError):
        return 0
    return 0


async def _scope_oom_killed(cap: ShellMemoryCap) -> bool:
    """True when the kernel killed something in the command's scope.

    systemd records ``Result=oom-kill`` once it has seen the kill. Until
    then the scope's cgroup still exists and its ``memory.events`` already
    counts it, so neither order loses the answer. A confirmed kill leaves a
    failed unit, which is cleared so it does not pile up in ``--failed``.
    """
    tools = cap.tools
    if tools is None:
        return False
    state = await _unit_state(cap, tools)
    killed = state.get("Result") == "oom-kill"
    if not killed:
        kills = await asyncio.to_thread(_cgroup_oom_kills, state.get("ControlGroup", ""))
        killed = kills > 0
    if not killed:
        return False
    for _ in range(_SETTLE_POLLS):
        if state.get("ActiveState") in ("failed", "inactive", ""):
            break
        await asyncio.sleep(_SETTLE_SECS)
        state = await _unit_state(cap, tools)
    if state.get("ActiveState") == "failed":
        await _systemctl(tools, "reset-failed", cap.unit)
    return True


def _note(cap: ShellMemoryCap) -> str:
    fix = "Use less memory, or raise caps.shell_memory_mb in .tst/config.yaml."
    if cap.method == "scope":
        return (
            f"memory cap: killed — the command went over its {cap.mb} MB memory cap "
            f"(caps.shell_memory_mb). {fix}"
        )
    if cap.method == "rlimit":
        return (
            f"memory cap: the command ran out of memory at its {cap.mb} MB cap "
            f"(caps.shell_memory_mb) and failed. {fix}"
        )
    return (
        f"memory cap: killed (exit {_SIGKILL_EXIT}) in a container capped at {cap.mb} MB "
        f"(caps.shell_memory_mb); going over that cap is the usual cause. {fix}"
    )


async def cap_note(cap: ShellMemoryCap | None, exit_code: int, stderr: str) -> str:
    """One line for the result when the cap stopped the command, else ``""``."""
    if cap is None:
        return ""
    if cap.method == "scope":
        hit = await _scope_oom_killed(cap)
    elif cap.method == "rlimit":
        hit = exit_code != 0 and ran_out_of_memory(stderr)
    else:
        hit = exit_code == _SIGKILL_EXIT
    return _note(cap) if hit else ""
