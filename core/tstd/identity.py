"""This computer's identity for other computers (TD-5015).

``machine_id`` is ``mid_`` plus 16 hex characters, minted once into
``{user_data_dir}/machine-id`` (0600, written atomically). It is not a
secret: it goes out in ``hello_ack`` and ``pair_ok`` after auth, so a
hub can tell "the same computer at a new address" and "this is me".
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import tempfile
from pathlib import Path

from .logging import get_logger

log = get_logger("tstd.identity")

MACHINE_ID_FILE = "machine-id"
MACHINE_ID_RE = re.compile(r"^mid_[0-9a-f]{16}$")


def mint_machine_id() -> str:
    """A fresh ``mid_<16hex>``."""
    return f"mid_{secrets.token_hex(8)}"


def load_or_mint_machine_id(data_dir: Path) -> str:
    """The id in ``machine-id``, minting and saving one the first time.

    A damaged file is replaced. A file that cannot be written still
    yields an id for this run (logged), so the daemon always starts;
    the next run mints again.
    """
    path = Path(data_dir) / MACHINE_ID_FILE
    try:
        current = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        current = ""
    if MACHINE_ID_RE.match(current):
        return current
    minted = mint_machine_id()
    try:
        _write_atomic(path, minted)
    except OSError as e:
        log.warning(
            "could not save the machine id; using one for this run",
            extra={"extra_fields": {"path": str(path), "error": e.strerror or type(e).__name__}},
        )
    return minted


def _write_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".machine-id.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
