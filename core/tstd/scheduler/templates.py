"""Job templates (TD-3816).

Built-ins ship in code. The user's own live at
``{data_dir}/scheduler/templates.json``. A template is not a job: the
instruction and the workspace may be blank, and a next-run rule such as
``tomorrow at 9:00`` is stored as the rule. Resolving it here would make
tomorrow's template stale the day after it was saved. The pane turns the
rule into an instant when the user picks the template.

The cadence is kept as the phrase the user would type. ``normalize_cadence``
only proves the phrase parses. Rewriting it to cron would fill the form
with ``45 7 * * 1-5`` instead of ``weekdays at 7:45``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..logging import get_logger, redact_secrets
from .grace import parse_grace
from .models import JobValidationError, normalize_cadence
from .retry import DEFAULT_RETRY_DELAY_SECONDS
from .template_check import (
    TemplateDraft,
    TemplateError,
    TemplateView,
    name_limit,
    normalize_rule,
    valid_id,
    validate_template,
)

__all__ = [
    "TemplateDraft",
    "TemplateError",
    "TemplateView",
    "delete_template",
    "list_templates",
    "save_template",
    "templates_path",
]

log = get_logger("tstd.scheduler.templates")

_ENVELOPE_VERSION = 1
_TEMPLATES_FILE = "templates.json"
# Not the jobs lock. A template save must not wait on a job write, and
# the other way around.
_LOCK = threading.RLock()


def _duration(phrase: str) -> int:
    parsed = parse_grace(phrase)
    if parsed is None:
        raise JobValidationError(f"{phrase} is not a duration")
    return parsed


def _builtin_digest() -> TemplateView:
    cadence = "weekdays at 7:45"
    normalize_cadence(cadence)
    return TemplateView(
        id="weekday-morning-digest",
        name="Weekday morning digest",
        builtin=True,
        instruction="",
        cadence=cadence,
        next_run=None,
        deliver_to="window",
        grace=_duration("2 hours"),
        retries=1,
        retry_delay=DEFAULT_RETRY_DELAY_SECONDS,
        max_run=None,
        preset=None,
        engine=None,
        workspace=None,
    )


def _builtin_reminder() -> TemplateView:
    return TemplateView(
        id="one-shot-reminder",
        name="One-shot reminder",
        builtin=True,
        instruction="",
        cadence=None,
        next_run=normalize_rule("tomorrow at 9:00"),
        deliver_to="window",
        grace=None,
        retries=0,
        retry_delay=None,
        max_run=None,
        preset=None,
        engine=None,
        workspace=None,
    )


_BUILTINS: tuple[TemplateView, ...] = (_builtin_digest(), _builtin_reminder())
BUILTIN_IDS = frozenset(row.id for row in _BUILTINS)


def templates_path(data_dir: str | Path) -> Path:
    """``{user_data_dir}/scheduler/templates.json``."""
    return Path(data_dir) / "scheduler" / _TEMPLATES_FILE


def list_templates(data_dir: str | Path) -> list[TemplateView]:
    """Built-ins first, then user rows. Does not create the file."""
    with _LOCK:
        return [*_BUILTINS, *_read_user(data_dir)]


def save_template(
    data_dir: str | Path,
    draft: TemplateDraft,
    catalog: Mapping[str, object],
    known_workspaces: Iterable[str] = (),
) -> list[TemplateView]:
    """Append one user template. Always a new id. Does not create a job."""
    view = validate_template(
        draft,
        template_id=uuid.uuid4().hex,
        builtin=False,
        catalog=catalog,
        known_workspaces=known_workspaces,
    )
    with _LOCK:
        rows = _read_user(data_dir)
        rows.append(view)
        _write(Path(data_dir), rows)
        return [*_BUILTINS, *rows]


def delete_template(data_dir: str | Path, template_id: str) -> list[TemplateView]:
    """Remove a user template. Built-ins stay, and the file is not rewritten."""
    if template_id in BUILTIN_IDS:
        raise TemplateError("template_builtin", "Built-in templates cannot be deleted")
    with _LOCK:
        rows = _read_user(data_dir)
        kept = [row for row in rows if row.id != template_id]
        if len(kept) == len(rows):
            raise TemplateError(
                "template_not_found",
                f"Template {_public_id(template_id)!r} not found",
            )
        _write(Path(data_dir), kept)
        return [*_BUILTINS, *kept]


def _public_id(template_id: str) -> str:
    """An id safe to put in an error. A secret-shaped id must not come back."""
    shown = redact_secrets(template_id)
    if len(shown) > name_limit():
        return shown[: name_limit()] + "…"
    return shown


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise JobValidationError("must be text")


def _as_measure(value: object) -> str | int | None:
    if value is None or isinstance(value, str | int):
        return value
    raise JobValidationError("must be a whole number or a duration")


def _read_user(data_dir: str | Path) -> list[TemplateView]:
    path = templates_path(data_dir)
    if not path.exists():
        return []
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # The message can quote the file. The file can hold a key.
        log.warning(
            "job templates unreadable, starting empty",
            extra={"extra_fields": {"path": str(path), "error_type": type(exc).__name__}},
        )
        return []
    rows = _rows(raw)
    if rows is None:
        return []
    kept: list[TemplateView] = []
    for entry in rows:
        view = _row_view(entry)
        if view is None:
            log.warning(
                "dropping malformed job template",
                extra={"extra_fields": {"path": str(path)}},
            )
            continue
        kept.append(view)
    return kept


def _rows(raw: Any) -> list[Any] | None:
    if not isinstance(raw, dict):
        return None
    version = raw.get("version", _ENVELOPE_VERSION)
    if not isinstance(version, int) or version != _ENVELOPE_VERSION:
        log.warning(
            "job templates version unsupported, starting empty",
            extra={"extra_fields": {"version": version if isinstance(version, int) else None}},
        )
        return None
    rows = raw.get("templates")
    return rows if isinstance(rows, list) else None


def _row_view(entry: object) -> TemplateView | None:
    if not isinstance(entry, dict):
        return None
    raw_id = entry.get("id")
    if not isinstance(raw_id, str) or raw_id in BUILTIN_IDS or not valid_id(raw_id):
        return None
    try:
        return validate_template(
            TemplateDraft(
                name=_as_text(entry.get("name")) or "",
                instruction=_as_text(entry.get("instruction")) or "",
                cadence=_as_text(entry.get("cadence")),
                next_run=_as_text(entry.get("next_run")),
                deliver_to=_as_text(entry.get("deliver_to")),
                grace=_as_measure(entry.get("grace")),
                retries=_as_measure(entry.get("retries")),
                retry_delay=_as_measure(entry.get("retry_delay")),
                max_run=_as_measure(entry.get("max_run")),
                preset=_as_text(entry.get("preset")),
                engine=_as_text(entry.get("engine")),
                workspace=_as_text(entry.get("workspace")),
                email_to=_as_text(entry.get("email_to")),
            ),
            template_id=raw_id,
            builtin=False,
            catalog=None,
            known_workspaces=(),
        )
    except JobValidationError:
        return None


def _dump(row: TemplateView) -> dict[str, Any]:
    payload = asdict(row)
    payload.pop("builtin", None)
    return payload


def _write(data_dir: Path, rows: list[TemplateView]) -> None:
    path = templates_path(data_dir)
    text = json.dumps(
        {"version": _ENVELOPE_VERSION, "templates": [_dump(row) for row in rows]},
        indent=2,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".templates.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Owner-only before the rename. POSIX mode bits only: on Windows
        # os.chmod can merely toggle the read-only flag (TD-1406).
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
