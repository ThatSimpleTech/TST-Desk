"""Run the pinned ``agent-desktop`` CLI as a subprocess (TD-5220).

``agent-desktop`` (Vercel Labs, Apache-2.0) reads and drives desktop apps
through the OS accessibility tree. Every command prints one JSON envelope:
``{"ok": true, "data": ...}`` or ``{"ok": false, "error": {...}}``. This
module owns the process boundary: which binary runs (checksum-pinned), with
what environment, and how a refusal becomes a :class:`DesktopError`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .protocol import DesktopError

#: The release these tools are built and tested against.
PINNED_RELEASE = "1.0.2"

#: SHA-256 of the ``agent-desktop`` binary inside each v1.0.2 release
#: archive. Each archive matched the release's ``checksums.txt`` first.
PINNED_SHA256: Mapping[str, str] = {
    "x86_64-unknown-linux-gnu": "05427281a6746489bc0e15c16698a70a86322125c76c7a9e2f4c2d7a12bf7c5b",
    "aarch64-unknown-linux-gnu": "529ae49006278f778bcda6b68d7460a55148e2cefb1cb69a23485381d9dbdec0",
    "aarch64-apple-darwin": "bf0a3c9256512f2f57288d37ccd17d7f03665fccc53c2f74011a49b98e61ba59",
    "x86_64-apple-darwin": "9684916bb4b5486c02cef8b2d480e55a551639b08358284f90a87cef43b2da8b",
    "x86_64-pc-windows-msvc": "409e58cbdd6aed47e29771ffa60bc122a9843505a227af488cded85215943988",
    "aarch64-pc-windows-msvc": "5877fbca41fb85806353d0aaf67c23cfdf06a58a77c5d9cba005727a2891034e",
}

#: Set by some desktop hosts and inherited by everything they start. A GTK
#: app launched with it never joins the accessibility bus, so it is
#: invisible to every snapshot.
_DROPPED_ENV = ("NO_AT_BRIDGE",)

#: Codes after which the ref is no good: take a new snapshot.
LOOK_AGAIN_CODES = frozenset({"STALE_REF", "SNAPSHOT_NOT_FOUND", "AMBIGUOUS_TARGET"})

#: Read-only commands. Re-running one is itself the "fresh snapshot" a
#: recovery may ask for, so a safe retry is allowed for these.
OBSERVATION_COMMANDS = frozenset({"snapshot", "list-windows", "list-apps", "get", "is", "find"})

# CLI error code → product code. Unlisted codes become ``ax_error``.
_CODE_MAP: Mapping[str, str] = {
    "STALE_REF": "ax_stale_ref",
    "SNAPSHOT_NOT_FOUND": "ax_stale_ref",
    "AMBIGUOUS_TARGET": "ax_ambiguous",
    "INVALID_ARGS": "ax_invalid_args",
    "POLICY_DENIED": "ax_not_supported",
    "ACTION_NOT_SUPPORTED": "ax_not_supported",
    "WINDOW_NOT_FOUND": "ax_not_found",
    "APP_NOT_FOUND": "ax_not_found",
    "ELEMENT_NOT_FOUND": "ax_not_found",
    "TIMEOUT": "ax_timeout",
}

_HINTS: Mapping[str, str] = {
    "ax_stale_ref": "The ref no longer matches the window. Take a new desktop_snapshot "
    "and use a ref from it.",
    "ax_ambiguous": "More than one window or element matches. Call desktop_windows and "
    "snapshot by window_id, or drill in with root.",
    "ax_not_supported": "That element cannot take this action without the pointer. "
    "Use the coordinate tools (desktop_click / desktop_type) instead.",
    "ax_not_found": "Nothing matches. Call desktop_windows to see what is open.",
    "ax_timeout": "The app did not answer in time. Some apps (browsers, web views) "
    "expose little or nothing; the coordinate tools still work there.",
}

Runner = Callable[[list[str], Mapping[str, str], float], Awaitable[tuple[int, bytes, bytes]]]


def host_target() -> str:
    """The release target triple for this machine, e.g. ``x86_64-unknown-linux-gnu``."""
    import platform

    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    if sys.platform == "win32":
        return f"{arch}-pc-windows-msvc"
    return f"{arch}-unknown-linux-gnu"


def child_env(base: Mapping[str, str], state_dir: Path | None) -> dict[str, str]:
    """The environment for the CLI and for every app it launches."""
    env = {key: value for key, value in base.items() if key not in _DROPPED_ENV}
    if state_dir is not None:
        # Snapshots hold screen text: keep them in TST Desk's own data dir.
        env["AGENT_DESKTOP_HOME"] = str(state_dir)
    return env


_NAME = "agent-desktop.exe" if sys.platform == "win32" else "agent-desktop"


def bundled_binary() -> Path | None:
    """The copy shipped beside the packaged daemon, when this is a packaged run."""
    if not getattr(sys, "frozen", False):
        return None
    beside = Path(sys.executable).resolve().parent / _NAME
    return beside if beside.is_file() else None


def resolve_binary(configured: str) -> Path | None:
    """The configured path, else beside the packaged daemon, else ``PATH``."""
    if configured:
        return Path(configured).expanduser()
    bundled = bundled_binary()
    if bundled is not None:
        return bundled
    found = shutil.which(_NAME)
    return Path(found) if found else None


def _identity(path: Path) -> tuple[int, int, int]:
    st = path.stat()
    return st.st_ino, st.st_size, st.st_mtime_ns


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _run_process(
    argv: list[str], env: Mapping[str, str], limit_seconds: float
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
    )
    try:
        async with asyncio.timeout(limit_seconds):
            out, err = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode if proc.returncode is not None else -1, out, err


if TYPE_CHECKING:
    from .ax_cursor import AxCursor


@dataclass(frozen=True)
class CliFailure:
    """A parsed ``ok: false`` envelope."""

    code: str
    message: str
    delivery: str
    retry: str
    retryable: bool
    fresh_snapshot: bool
    retry_after_ms: int
    candidates: tuple[dict[str, Any], ...]

    @classmethod
    def from_error(cls, error: Mapping[str, Any]) -> CliFailure:
        disposition = error.get("disposition")
        disp = disposition if isinstance(disposition, dict) else {}
        recovery = error.get("recovery")
        rec = recovery if isinstance(recovery, dict) else {}
        details = error.get("details")
        det = details if isinstance(details, dict) else {}
        raw_candidates = det.get("candidates")
        candidates = tuple(
            {k: c[k] for k in ("id", "title") if k in c}
            for c in (raw_candidates if isinstance(raw_candidates, list) else [])[:10]
            if isinstance(c, dict)
        )
        after = rec.get("retry_after_ms")
        return cls(
            code=str(error.get("code") or "UNKNOWN"),
            message=str(error.get("message") or ""),
            delivery=str(disp.get("delivery") or "unknown"),
            retry=str(disp.get("retry") or "unknown"),
            retryable=bool(rec.get("retryable")) and det.get("retryable") is not False,
            fresh_snapshot=bool(rec.get("requires_fresh_snapshot")),
            retry_after_ms=after if isinstance(after, int) and 0 <= after <= 2000 else 250,
            candidates=candidates,
        )

    def may_retry(self, command: str) -> bool:
        """Retry only a refusal the CLI itself calls safe and worth repeating."""
        if self.retry != "safe" or not self.retryable:
            return False
        if self.code in LOOK_AGAIN_CODES or self.code == "INVALID_ARGS":
            return False
        return command in OBSERVATION_COMMANDS or not self.fresh_snapshot

    def to_error(self) -> DesktopError:
        product = _CODE_MAP.get(self.code, "ax_error")
        parts = [f"{self.code}: {self.message}".strip()]
        hint = _HINTS.get(product)
        if hint:
            parts.append(hint)
        if self.candidates:
            listed = "; ".join(
                f"{c.get('id', '?')} {json.dumps(c.get('title', ''))}" for c in self.candidates
            )
            parts.append(f"Candidates: {listed}.")
        if self.delivery not in ("not_delivered", "unknown"):
            parts.append(
                f"Delivery was {self.delivery!r}: the action may have landed. "
                "Read the state with desktop_snapshot before repeating it."
            )
        return DesktopError(product, " ".join(parts))


class AgentDesktopCli:
    """One checksum-verified ``agent-desktop`` binary, run per command."""

    def __init__(
        self,
        binary: Path | None,
        *,
        sha256: str = "",
        state_dir: Path | None = None,
        timeout_seconds: float = 20.0,
        runner: Runner | None = None,
        base_env: Mapping[str, str] | None = None,
        packaged: bool = False,
    ) -> None:
        self._binary = binary
        self._sha256 = sha256.lower()
        # The copy shipped inside the app package. The AppImage tool rewrites
        # the binaries it packs, so the release digest cannot match there; the
        # package is as trusted as the daemon running from it. An explicit
        # ``sha256`` still applies.
        self._packaged = packaged
        self._state_dir = state_dir
        self._timeout = timeout_seconds
        self._runner: Runner = runner if runner is not None else _run_process
        self._env = child_env(os.environ if base_env is None else base_env, state_dir)
        # (inode, size, mtime) of the file last hashed: a changed file is hashed again.
        self._verified: tuple[int, int, int] | None = None
        # agent-desktop's per-agent cursor (TD-5227); None runs without it.
        self.cursor: AxCursor | None = None

    @property
    def env(self) -> Mapping[str, str]:
        return self._env

    def accepted_digests(self) -> frozenset[str]:
        if self._sha256:
            return frozenset({self._sha256})
        return frozenset(PINNED_SHA256.values())

    async def _verify(self) -> Path:
        if self._binary is None:
            raise DesktopError(
                "ax_unavailable",
                "The agent-desktop CLI was not found. Set "
                "computer_use.accessibility.binary to its path.",
            )
        binary = self._binary
        try:
            identity = await asyncio.to_thread(_identity, binary)
            if identity == self._verified:
                return binary
            digest = await asyncio.to_thread(_sha256, binary)
        except OSError as exc:
            raise DesktopError(
                "ax_unavailable", f"The agent-desktop CLI could not be read: {exc.strerror}."
            ) from exc
        if digest not in self.accepted_digests() and not (self._packaged and not self._sha256):
            raise DesktopError(
                "ax_checksum",
                f"The agent-desktop CLI at {binary} does not match the pinned checksum "
                f"(release {PINNED_RELEASE}). It was not run.",
            )
        if self._state_dir is not None:
            await asyncio.to_thread(self._state_dir.mkdir, mode=0o700, parents=True, exist_ok=True)
        self._verified = identity
        return binary

    async def run(self, command: str, *args: str) -> Any:
        """Run one command as the acting session's agent. Refusals raise ``DesktopError``."""
        prefix = await self.cursor.scope(self) if self.cursor is not None else []
        return await self.run_with(prefix, command, *args)

    async def run_with(self, prefix: list[str], command: str, *args: str) -> Any:
        """Run one command after global flags (``--session``, ``--agent-id``)."""
        binary = await self._verify()
        argv = [str(binary), *prefix, command, *args]
        result = await self._once(argv)
        if isinstance(result, CliFailure):
            if not result.may_retry(command):
                raise result.to_error()
            await asyncio.sleep(result.retry_after_ms / 1000)
            result = await self._once(argv)
            if isinstance(result, CliFailure):
                raise result.to_error()
        return result.data

    async def _once(self, argv: list[str]) -> CliFailure | _Ok:
        try:
            _code, out, _err = await self._runner(argv, self._env, self._timeout)
        except TimeoutError as exc:
            raise DesktopError(
                "ax_timeout", f"agent-desktop did not answer within {self._timeout:g} s."
            ) from exc
        except OSError as exc:
            raise DesktopError(
                "ax_unavailable", f"agent-desktop could not start: {exc.strerror}."
            ) from exc
        try:
            envelope = json.loads(out.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            # stderr can quote window text; it is not echoed.
            raise DesktopError("ax_error", "agent-desktop printed no JSON envelope.") from exc
        if not isinstance(envelope, dict):
            raise DesktopError("ax_error", "agent-desktop printed an unexpected envelope.")
        if envelope.get("ok") is True:
            return _Ok(envelope.get("data"))
        error = envelope.get("error")
        return CliFailure.from_error(error if isinstance(error, dict) else {})


@dataclass(frozen=True)
class _Ok:
    data: Any
