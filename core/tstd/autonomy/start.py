"""Sign-and-start gate for autonomous runs (TD-4003).

The start button is a human verb: write the charter if the pane sent
one, commit it (the sign), then refuse unless the charter, the
sandbox, and a git repo (TD-4102) are clean. The daemon launches the
unattended loop (TD-4101) only after this gate returns ready.
Interactive sessions never call this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AutonomyConfig, ModelConfig
from .charter import Charter, CharterError, charter_path, charter_start_error, sign_charter
from .charter_io import read_charter_document, write_charter_document
from .checkpoint import checkpoint_start_error
from .sandbox import InspectFn, WhichFn, sandbox_start_error


@dataclass(frozen=True)
class AutonomyStartResult:
    """Outcome of one start-button press."""

    ready: bool
    signed: bool
    error: str | None
    charter: Charter | None
    notes: str


async def run_autonomy_start(
    workspace: str | Path,
    *,
    config: AutonomyConfig | ModelConfig,
    charter: dict[str, Any] | None = None,
    notes: str = "",
    which: WhichFn | None = None,
    inspect: InspectFn | None = None,
) -> AutonomyStartResult:
    """Sign the charter and refuse unless the sandbox is live.

    ``charter`` is the same loose mapping ``save_charter`` takes. When
    omitted, the on-disk file is signed as-is. A write or sign failure
    is ``ready=False``; ``CharterError`` from the write is raised for
    the daemon to map to ``invalid_charter``.
    """
    ws = Path(workspace)
    if charter is not None:
        await asyncio.to_thread(write_charter_document, ws, charter, notes)
    if charter_path(ws).is_file():
        git_err = await checkpoint_start_error(ws)
        if git_err is not None:
            return AutonomyStartResult(
                ready=False, signed=False, error=git_err, charter=None, notes=""
            )
    sign_err = await sign_charter(ws)
    if sign_err is not None:
        return AutonomyStartResult(
            ready=False, signed=False, error=sign_err, charter=None, notes=""
        )
    gate = await charter_start_error(ws)
    if gate is not None:
        return AutonomyStartResult(ready=False, signed=True, error=gate, charter=None, notes="")
    sand = await sandbox_start_error(config, which=which, inspect=inspect)
    if sand is not None:
        return AutonomyStartResult(ready=False, signed=True, error=sand, charter=None, notes="")
    try:
        _present, parsed, stored_notes = await asyncio.to_thread(read_charter_document, ws)
    except CharterError as e:
        return AutonomyStartResult(ready=False, signed=True, error=str(e), charter=None, notes="")
    return AutonomyStartResult(
        ready=True,
        signed=True,
        error=None,
        charter=parsed,
        notes=stored_notes,
    )
