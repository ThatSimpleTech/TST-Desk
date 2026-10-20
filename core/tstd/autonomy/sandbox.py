"""Rootless container gate for autonomous runs (TD-4301).

Spec §12.5 and TD-101: autonomy is hard-required to run in a container
with only the workspace bound in. This module is the isolation primitive
— probe the configured runtime, refuse start with install copy when it
is missing or not rootless, and build the argv the autonomy *shell*
tool execs. Filesystem tools stay host-side behind PathGuard (the
workspace *is* the mount). Interactive sessions never import this
module from the loop. No Firecracker path (follow-up).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..config import AutonomyConfig, ModelConfig
from ..host_memory import default_shell_memory_mb
from ..proc_lifecycle import finish_subprocess

WORKSPACE_DEST = "/workspace"
_INFO_TIMEOUT_SECONDS = 8.0
_EXEC_TIMEOUT_SECONDS = 30.0

INSTALL_MISSING = (
    "Autonomy requires a rootless container runtime. Install Podman and "
    "put the binary named in autonomy.runtime on PATH. On macOS: brew "
    "install podman, then podman machine init && podman machine start. "
    "Interactive sessions do not need this."
)
INSTALL_DOWN = (
    "The container runtime is installed but not live. On macOS run "
    "`podman machine start`. Interactive sessions do not need this."
)
INSTALL_ROOTFUL = (
    "The container runtime is running rootful. Autonomy requires rootless "
    "Podman. Interactive sessions do not need this."
)
INSTALL_NO_LIMITS = (
    "Rootless Podman here cannot cap a container's memory "
    "(caps.shell_memory_mb): that needs cgroup v2 and the systemd cgroup "
    'manager (cgroup_manager = "systemd" in containers.conf). Without '
    "them an unattended run would have no memory cap. Interactive sessions "
    "do not need this."
)

WhichFn = Callable[[str], str | None]


class RuntimeState(StrEnum):
    """What the configured runtime probe observed."""

    MISSING = "missing"
    DOWN = "down"
    ROOTFUL = "rootful"
    ROOTLESS = "rootless"
    #: Rootless, but on cgroup v1 or the cgroupfs manager, where Podman
    #: ignores or refuses ``--memory`` (TD-5160).
    NO_LIMITS = "no_limits"


InspectFn = Callable[[str], Awaitable[RuntimeState]]


class SandboxError(Exception):
    """Raised when a sandbox argv cannot be built or an exec is refused."""


@dataclass(frozen=True)
class SandboxExec:
    """Result of one ``runtime run`` (tests and ``sandbox_exec``)."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


def _autonomy_of(config: AutonomyConfig | ModelConfig) -> AutonomyConfig:
    return config.autonomy if isinstance(config, ModelConfig) else config


def resolve_runtime(runtime: str, *, which: WhichFn) -> Path | None:
    """Locate the configured runtime, or ``None`` when it is not executable."""
    name = runtime.strip()
    if not name:
        return None
    if os.sep in name or name.startswith("."):
        path = Path(name).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
        return None
    found = which(name)
    if found is None:
        return None
    path = Path(found)
    if path.is_file() and os.access(path, os.X_OK):
        return path.resolve()
    return None


async def inspect_runtime(binary: str) -> RuntimeState:
    """Ask ``binary info`` whether the engine is up, rootless, and can cap memory.

    The format string is Podman's. An empty, unknown, or failing reply is
    ``down`` — a state we cannot prove rootless is not live. Rootless
    Podman sets resource limits only on cgroup v2 with the systemd cgroup
    manager; elsewhere ``--memory`` is ignored or refused (TD-5160).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "info",
            "--format",
            "{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}} {{.Host.CgroupManager}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return RuntimeState.DOWN
    try:
        out, _err = await finish_subprocess(proc, timeout=_INFO_TIMEOUT_SECONDS)
    except TimeoutError:
        return RuntimeState.DOWN
    if proc.returncode != 0:
        return RuntimeState.DOWN
    tokens = out.decode("utf-8", errors="replace").strip().strip('"').lower().split()
    if tokens[:1] == ["false"]:
        return RuntimeState.ROOTFUL
    if tokens[:1] != ["true"]:
        return RuntimeState.DOWN
    if tokens[1:] != ["v2", "systemd"]:
        return RuntimeState.NO_LIMITS
    return RuntimeState.ROOTLESS


def _which(which: WhichFn | None) -> WhichFn:
    return which if which is not None else shutil.which


async def probe_runtime(
    config: AutonomyConfig | ModelConfig,
    *,
    which: WhichFn | None = None,
    inspect: InspectFn | None = None,
) -> RuntimeState:
    """Resolve the configured runtime and classify it."""
    autonomy = _autonomy_of(config)
    resolved = resolve_runtime(autonomy.runtime, which=_which(which))
    if resolved is None:
        return RuntimeState.MISSING
    fn = inspect if inspect is not None else inspect_runtime
    return await fn(str(resolved))


async def sandbox_start_error(
    config: AutonomyConfig | ModelConfig,
    *,
    which: WhichFn | None = None,
    inspect: InspectFn | None = None,
) -> str | None:
    """Why an autonomous run may not start, or ``None`` when the sandbox is live.

    Interactive sessions never call this. Compose with ``charter_start_error``
    at the start button (TD-4003).
    """
    state = await probe_runtime(config, which=_which(which), inspect=inspect)
    if state is RuntimeState.MISSING:
        return INSTALL_MISSING
    if state is RuntimeState.DOWN:
        return INSTALL_DOWN
    if state is RuntimeState.ROOTFUL:
        return INSTALL_ROOTFUL
    if state is RuntimeState.NO_LIMITS:
        return INSTALL_NO_LIMITS
    return None


def _network_denied(network: str | list[str]) -> bool:
    """True when the charter wall forbids container egress.

    Same shape as ``BoundarySection.network``: ``"deny"`` or a host list.
    A non-empty list of non-blank hosts is the only shape that opens
    slirp. Empty lists, other strings, and mixed junk fail closed as
    deny — CNI cannot punch per-host holes, so unexpected values must
    not widen the net ns.
    """
    return not (
        isinstance(network, list)
        and bool(network)
        and all(isinstance(host, str) and host.strip() for host in network)
    )


def container_argv(
    workspace: Path,
    *,
    runtime: str,
    image: str,
    inner: Sequence[str],
    network: str | list[str] = "deny",
    memory_mb: int | None = None,
) -> list[str]:
    """Argv that execs *inner* with only *workspace* bind-mounted.

    The only ``--mount`` is the workspace. There is no extra-mount
    argument — host ``$HOME``, keychain, ``~/.ssh``, cloud creds, and
    the user-data-dir cannot be bound in. ``network="deny"`` (the
    default) is ``--network=none``. A host allowlist omits that flag so
    Podman's default slirp can reach tool-level hosts; it never uses
    ``--network=host``. Unexpected values fail closed as deny.
    ``--pull=never`` so a start check cannot phone a registry.
    *memory_mb* is ``--memory`` (TD-5116); the runners below always pass
    one when RAM is known.
    """
    if not inner:
        raise SandboxError("container command is empty")
    if not image.strip():
        raise SandboxError("autonomy.image is empty")
    if not runtime.strip():
        raise SandboxError("autonomy.runtime is empty")
    ws = workspace.resolve()
    ws_text = str(ws)
    if "," in ws_text:
        raise SandboxError("workspace path cannot contain comma")
    if sys.platform == "win32":
        # Drive-absolute C:\… is required on Windows; drive-relative C:foo is not.
        if len(ws_text) >= 2 and ws_text[1] == ":" and ws_text[2:3] not in ("\\", "/"):
            raise SandboxError("workspace path cannot contain drive-relative colon form")
    elif ":" in ws_text:
        raise SandboxError("workspace path cannot contain colon")
    argv = [runtime, "run", "--rm"]
    if _network_denied(network):
        argv.append("--network=none")
    if memory_mb is not None:
        if memory_mb < 1:
            raise SandboxError("container memory cap must be at least 1 MB")
        # Swap equal to the cap is no swap, as the host scope's
        # MemorySwapMax=0: Podman's default lets a container swap as much
        # again, which is the slow death the cap exists to stop (TD-5160).
        argv.extend([f"--memory={memory_mb}m", f"--memory-swap={memory_mb}m"])
    argv.extend(
        [
            "--userns=keep-id",
            "--security-opt",
            "no-new-privileges",
            "--pull",
            "never",
            "--mount",
            f"type=bind,src={ws},dst={WORKSPACE_DEST}",
            "--workdir",
            WORKSPACE_DEST,
            image,
            *inner,
        ]
    )
    return argv


def autonomy_shell_argv(
    workspace: Path,
    command: str,
    *,
    runtime: str,
    image: str,
    network: str | list[str] = "deny",
    which: WhichFn | None = None,
    memory_mb: int | None = None,
) -> list[str]:
    """Argv that runs *command* inside the sandbox via ``/bin/sh -c``.

    The inner shell is the *image's* ``/bin/sh``, not the host's. Used by
    the product shell tool on autonomous sessions (TD-4301). *memory_mb*
    is the session's ``caps.shell_memory_mb``; None is half of RAM.
    """
    if not command.strip():
        raise SandboxError("container command is empty")
    resolved = resolve_runtime(runtime, which=_which(which))
    if resolved is None:
        raise SandboxError(INSTALL_MISSING)
    return container_argv(
        workspace,
        runtime=str(resolved),
        image=image,
        inner=["/bin/sh", "-c", command],
        network=network,
        memory_mb=memory_mb if memory_mb is not None else default_shell_memory_mb(),
    )


async def sandbox_exec(
    workspace: Path,
    inner: Sequence[str],
    *,
    config: AutonomyConfig | ModelConfig,
    which: WhichFn | None = None,
    inspect: InspectFn | None = None,
    network: str | list[str] = "deny",
    memory_mb: int | None = None,
) -> SandboxExec:
    """Run *inner* inside the sandbox, or raise :class:`SandboxError`.

    *memory_mb* is ``--memory``; None is half of RAM (TD-5116).
    """
    autonomy = _autonomy_of(config)
    err = await sandbox_start_error(autonomy, which=which, inspect=inspect)
    if err is not None:
        raise SandboxError(err)
    resolved = resolve_runtime(autonomy.runtime, which=_which(which))
    if resolved is None:
        raise SandboxError(INSTALL_MISSING)
    argv = container_argv(
        workspace,
        runtime=str(resolved),
        image=autonomy.image,
        inner=inner,
        network=network,
        memory_mb=memory_mb if memory_mb is not None else default_shell_memory_mb(),
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise SandboxError(f"failed to exec sandbox runtime: {exc}") from exc
    try:
        out, err_b = await finish_subprocess(proc, timeout=_EXEC_TIMEOUT_SECONDS)
    except TimeoutError:
        raise SandboxError("sandbox exec timed out") from None
    return SandboxExec(
        argv=argv,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err_b.decode("utf-8", errors="replace"),
    )
