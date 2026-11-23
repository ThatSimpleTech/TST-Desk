"""The sandbox seam (TD-5294): one backend interface and a reported level.

Enforcement is a fact a backend reports after it has been tried, not a
promise read from a version string. The dispatcher only lets a command run
unasked when the level is ``full``, the approval card says what the command
would run in, and Doctor shows the backend, its level and every gap.
"""

from .backend import (
    PROMISES,
    BackendStatus,
    Enforcement,
    SandboxBackend,
    cached_status,
    current_status,
    reprobe,
    set_backend,
)

__all__ = [
    "PROMISES",
    "BackendStatus",
    "Enforcement",
    "SandboxBackend",
    "cached_status",
    "current_status",
    "reprobe",
    "set_backend",
]
