"""Bubblewrap backend probe (TD-5294).

The argv itself is still built by ``shell_sandbox.build_plan``; this module
only says, by trying it, whether that argv can run here and what it holds.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys

from .backend import BackendStatus, Enforcement

_TIMEOUT = 10
# A user namespace that maps root inside the sandbox has the capabilities to
# remount a read-only bind, so the pins would not hold (TD-5264).
_NESTED = "unshare -Ur true"


def _run(argv: list[str]) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(argv, capture_output=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


class BwrapBackend:
    name = "bwrap"

    def __init__(self, bwrap: str = "bwrap") -> None:
        self._bwrap = bwrap

    def _none(self, why: str) -> BackendStatus:
        return BackendStatus(self.name, Enforcement.NONE, unavailable=why)

    def probe(self) -> BackendStatus:
        # Imported here: shell_sandbox imports this package for its status.
        from ..shell_sandbox import seccomp_supported

        if not sys.platform.startswith("linux"):
            return BackendStatus(
                "none",
                Enforcement.NONE,
                unavailable="the shell sandbox needs Linux (bubblewrap) for now; "
                "turn sandbox.enabled off here",
            )
        if not seccomp_supported():
            return self._none(
                f"the shell sandbox supports x86_64 and aarch64, not {platform.machine()}; "
                "turn sandbox.enabled off here"
            )
        found = shutil.which(self._bwrap)
        if found is None:
            return self._none(
                "bubblewrap is not installed; install it (apt install bubblewrap / "
                "dnf install bubblewrap) or turn sandbox.enabled off"
            )
        base = [found, "--die-with-parent", "--unshare-all", "--ro-bind", "/", "/"]
        base += ["--dev", "/dev", "--proc", "/proc", "--"]
        tried = _run([*base, "/bin/true"])
        if tried is None or tried.returncode != 0:
            said = tried.stderr.decode(errors="replace").strip()[:200] if tried else "no answer"
            return self._none(
                f"bubblewrap is installed but could not start a sandbox here ({said}); "
                "check that unprivileged user namespaces are allowed, or turn "
                "sandbox.enabled off"
            )
        nested = _run([*base, "/bin/sh", "-c", _NESTED])
        if nested is None or nested.returncode == 127:
            return BackendStatus(
                self.name,
                Enforcement.PARTIAL,
                gaps=(
                    ("pins", "whether a command can undo a read-only bind could not be checked"),
                ),
            )
        if nested.returncode == 0:
            return BackendStatus(
                self.name,
                Enforcement.PARTIAL,
                gaps=(
                    (
                        "pins",
                        "a command can make a user namespace of its own here and "
                        "undo a read-only bind",
                    ),
                ),
            )
        return BackendStatus(self.name, Enforcement.FULL)
