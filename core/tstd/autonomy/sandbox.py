"""Rootless container gate for autonomous runs (TD-4301).

Spec §12.5 and TD-101: autonomy is hard-required to run in a container
with only the workspace bound in. This module is the isolation primitive
— probe the configured runtime, refuse start with install copy when it
is missing or not rootless, and build the argv a later runner execs.

Interactive sessions must not import this module. There is no start
button here (TD-4003) and no Firecracker path (follow-up).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..config import AutonomyConfig, ModelConfig

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

WhichFn = Callable[[str], str | None]


class RuntimeState(StrEnum):
    """What the configured runtime probe observed."""

    MISSING = "missing"
    DOWN = "down"
    ROOTFUL = "rootful"
    ROOTLESS = "rootless"


InspectFn = Callable[[str], Awaitable[RuntimeState]]


class SandboxError(Exception):
    """Raised when a sandbox argv cannot be built or an exec is refused."""


@dataclass(frozen=True)
class SandboxExec:
    """Result of one ``runtime run`` (used by tests and, later, TD-4101)."""

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
    """Ask ``binary info`` whether the engine is up and rootless.

    The format string is Podman's. An empty, unknown, or failing reply is
    ``down`` — a state we cannot prove rootless is not live.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "info",
            "--format",
            "{{.Host.Security.Rootless}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return RuntimeState.DOWN
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=_INFO_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return RuntimeState.DOWN
    if proc.returncode != 0:
        return RuntimeState.DOWN
    token = out.decode("utf-8", errors="replace").strip().strip('"').lower()
    if token == "true":
        return RuntimeState.ROOTLESS
    if token == "false":
        return RuntimeState.ROOTFUL
    return RuntimeState.DOWN


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
    return None


def container_argv(
    workspace: Path,
    *,
    runtime: str,
    image: str,
    inner: Sequence[str],
) -> list[str]:
    """Argv that execs *inner* with only *workspace* bind-mounted.

    ``--network=none`` until the charter wall can punch holes (TD-4302).
    ``--pull=never`` so a start check cannot phone a registry. No
    ``$HOME``, no host network, no privileged flag.
    """
    if not inner:
        raise SandboxError("container command is empty")
    if not image.strip():
        raise SandboxError("autonomy.image is empty")
    if not runtime.strip():
        raise SandboxError("autonomy.runtime is empty")
    ws = workspace.resolve()
    if any(ch in str(ws) for ch in ",:"):
        raise SandboxError("workspace path cannot contain comma or colon")
    return [
        runtime,
        "run",
        "--rm",
        "--network=none",
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


async def sandbox_exec(
    workspace: Path,
    inner: Sequence[str],
    *,
    config: AutonomyConfig | ModelConfig,
    which: WhichFn | None = None,
    inspect: InspectFn | None = None,
) -> SandboxExec:
    """Run *inner* inside the sandbox, or raise :class:`SandboxError`."""
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
        async with asyncio.timeout(_EXEC_TIMEOUT_SECONDS):
            out, err_b = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise SandboxError("sandbox exec timed out") from None
    return SandboxExec(
        argv=argv,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err_b.decode("utf-8", errors="replace"),
    )
