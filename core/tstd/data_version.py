"""Which version wrote this data folder, and read-only mode when it was a newer one (UPGRADE-02).

``data_version.json`` in the data folder names the folder's format and the
version that last ran on it. A build that finds a newer format than its own
starts read-only: the person can read and copy their chats, a banner says to
update, and nothing in the folder changes until then. Log files are still
written. A missing stamp is an older folder (or a new one) and is stamped.

Every store writer calls :func:`guard_write` first; in read-only mode it
raises :class:`DataReadOnly`, which callers that run in the background
treat as "skip", and the protocol turns into a ``data_read_only`` refusal.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .logging import get_logger

log = get_logger("tstd.data_version")

#: This build's data format. Raise it when a store changes in a way an older
#: build would misread; an older build then opens the folder read-only.
DATA_FORMAT = 1
STAMP_NAME = "data_version.json"


@dataclass(frozen=True)
class NewerData:
    """The folder was written by a newer format than this build reads."""

    format: int
    written_by: str


#: Client messages that change nothing in the data folder, so they work in
#: read-only mode: reading, copying out, stopping and quitting.
READ_ONLY_MESSAGES = frozenset(
    {
        "hello",
        "attach",
        "detach",
        "get_setup_state",
        "get_usage",
        "export_usage",
        "get_charter",
        "get_config_editor",
        "get_hooks",
        "get_instruction_stack",
        "get_preset_roles",
        "get_session_job_source",
        "list_artifacts",
        "list_commands",
        "list_endpoint_models",
        "list_grok_extensions",
        "list_grok_sessions",
        "list_instructions",
        "list_job_runs",
        "list_job_templates",
        "list_jobs",
        "list_machines",
        "list_memory",
        "list_pins",
        "list_policy_rules",
        "list_sessions",
        "preview_mcp_import",
        "preview_restore_files",
        "read_artifact_chunk",
        "search_sessions",
        "check_cu_permissions",
        "check_presets",
        "cancel_pairing",
        "cancel",
        "stop_turn",
        "worker_stop",
        "shutdown",
        "drain",
        "run_diagnostics",
        "open_artifact",
        "open_in_terminal",
        "design_hit_test",
        "parse_job",
        "test_model",
        "validate_api_key",
        "scan_local_servers",
        "extract_attachment",
        "transcribe",
    }
)


class DataReadOnly(RuntimeError):
    """A write was refused: the data folder belongs to a newer version."""


_read_only: NewerData | None = None


def read_only() -> NewerData | None:
    """Why this process may not change its data folder, or None."""
    return _read_only


def set_read_only(newer: NewerData | None) -> None:
    global _read_only
    _read_only = newer


def guard_write(what: str) -> None:
    """Raise :class:`DataReadOnly` when the data folder must not change."""
    if _read_only is not None:
        raise DataReadOnly(
            f"{what} was not saved: this data folder was written by TST Desk "
            f"{_read_only.written_by}. Update TST Desk to keep working."
        )


def check_and_stamp(data_dir: Path, version: str) -> NewerData | None:
    """Read the stamp; a newer format is returned and left as it is, else stamp ours."""
    path = data_dir / STAMP_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = None
    except (OSError, ValueError) as exc:
        log.warning("data_version.json unreadable", extra={"extra_fields": {"error": str(exc)}})
        raw = None
    if isinstance(raw, dict):
        found = raw.get("format")
        if isinstance(found, int) and not isinstance(found, bool) and found > DATA_FORMAT:
            by = raw.get("written_by")
            return NewerData(found, by if isinstance(by, str) else "a newer version")
        if raw.get("format") == DATA_FORMAT and raw.get("written_by") == version:
            return None
    _write_stamp(path, version)
    return None


def _write_stamp(path: Path, version: str) -> None:
    text = json.dumps({"format": DATA_FORMAT, "written_by": version}) + "\n"
    tmp = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".data_version.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        log.warning("could not stamp the data folder", extra={"extra_fields": {"error": str(exc)}})
