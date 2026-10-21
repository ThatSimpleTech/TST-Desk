"""Problems a language server finds in a file the agent just wrote (TD-5139).

After a successful ``fs_write`` or ``fs_edit``, each written file that a
configured server matches is sent to that server (opened, or its whole
new text), and the diagnostics it publishes come back on the same tool
result, so a type error costs no extra turn.

Only errors and warnings are reported, and only the new ones: a problem
this session's model was already told about for that file is not repeated (matched
on severity, code and message, since an edit above it moves its line).
When the last problems are gone, the note says so. A clean file that was
clean before adds nothing. A server that does not answer within
``diagnostics_wait_seconds`` adds nothing either; one that cannot start
says so once.

The server is the user's own configured program, not a tool call the
model made, so starting it does not go through the decision classifier,
like an ``after_write`` hook. It only sees files a classified, approved
write already changed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..logging import get_logger
from . import LspError, LspStartFailed, Target, default_pool, lsp_config, target_for
from .positions import uri_key

log = get_logger(__name__)

_SEVERITY = {1: "error", 2: "warning"}


def _key(d: dict[str, Any]) -> tuple[int, str, str]:
    return (int(d.get("severity") or 1), str(d.get("code") or ""), str(d.get("message") or ""))


def reportable(diagnostics: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Errors and warnings only (no severity counts as an error)."""
    return [d for d in diagnostics if int(d.get("severity") or 1) in _SEVERITY]


def new_since(
    now: Sequence[dict[str, Any]], before: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The entries of *now* not already in *before*, counting duplicates."""
    left = Counter(_key(d) for d in before)
    fresh: list[dict[str, Any]] = []
    for d in now:
        k = _key(d)
        if left[k] > 0:
            left[k] -= 1
        else:
            fresh.append(d)
    return fresh


def format_diagnostic(rel: str, d: dict[str, Any]) -> str:
    """``rel:line:col severity: message (code)`` with 1-based positions."""
    start = (d.get("range") or {}).get("start") or {}
    line = int(start.get("line", 0)) + 1
    col = int(start.get("character", 0)) + 1
    severity = _SEVERITY.get(int(d.get("severity") or 1), "error")
    message = " ".join(str(d.get("message") or "").split())
    code = d.get("code")
    tail = f" ({code})" if code not in (None, "") else ""
    return f"  {rel}:{line}:{col} {severity}: {message}{tail}"


def _sort_key(d: dict[str, Any]) -> tuple[int, int, int]:
    start = (d.get("range") or {}).get("start") or {}
    return (int(d.get("severity") or 1), int(start.get("line", 0)), int(start.get("character", 0)))


def note_for(
    label: str, rel: str, now: list[dict[str, Any]], before: list[dict[str, Any]], cap: int
) -> str:
    """What the model is told about one file, or "" for nothing."""
    fresh = sorted(new_since(now, before), key=_sort_key)
    if fresh:
        shown = fresh[:cap]
        more = len(fresh) - len(shown)
        older = len(now) - len(fresh)
        head = f"[language server `{label}`: {len(fresh)} new problem(s) in {rel}"
        if older:
            head += f"; {older} reported before still there"
        lines = [head + "]", *(format_diagnostic(rel, d) for d in shown)]
        if more:
            lines.append(f"  ... and {more} more")
        return "\n".join(lines)
    if before and len(now) < len(before):
        if not now:
            return f"[language server `{label}`: no problems left in {rel}]"
        return (
            f"[language server `{label}`: {len(before) - len(now)} problem(s) in {rel} fixed; "
            f"{len(now)} left]"
        )
    return ""


Reported = dict[tuple[str, str], list[dict[str, Any]]]


def reported_for(session: object) -> Reported:
    """What *session*'s model was last told, per server and file.

    Kept on the session, not on the pooled client: sessions in the same
    workspace share one server, but each model only knows what its own
    tool results said, so one session's report must not hide a problem
    from another.
    """
    store = getattr(session, "lsp_reported", None)
    if isinstance(store, dict):
        return store
    store = {}
    with contextlib.suppress(AttributeError):
        session.lsp_reported = store  # type: ignore[attr-defined]
    return store


async def _one(target: Target, path: Path, reported: Reported) -> str:
    label = target.server.label
    try:
        client = await default_pool().client(target.config, target.server, target.root)
    except LspStartFailed as e:
        return f"[language server `{label}` could not start: {e}]" if e.first else ""
    try:
        text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
    except OSError:
        return ""
    uri = path.as_uri()
    after = client.publish_count(uri)
    try:
        await client.sync(path, text, saved=True)
    except LspError as e:
        return f"[language server `{label}` stopped: {e}]"
    published = await client.wait_for_diagnostics(
        uri, after, target.server.diagnostics_wait_seconds
    )
    if published is None:
        log.info(
            "language server sent no diagnostics in time",
            extra={"extra_fields": {"server": label, "file": target.rel}},
        )
        return ""
    now = reportable(published)
    key = (label, uri_key(uri))
    before = reported.get(key, [])
    reported[key] = now
    return note_for(label, target.rel, now, before, target.config.max_diagnostics)


async def diagnostics_after_write(session: object, files: Sequence[Path]) -> str:
    """The note for the model about every written file a server matches.

    Never raises: a server that breaks is a note or nothing, and the
    write itself has already succeeded.
    """
    if lsp_config(session) is None:
        return ""
    notes: list[str] = []
    reported = reported_for(session)
    for path in files:
        try:
            target = await target_for(session, Path(path))
            if target is None or target.server.diagnostics_wait_seconds <= 0:
                continue
            note = await _one(target, target.path, reported)
        except Exception:
            log.exception("language server diagnostics failed")
            continue
        if note:
            notes.append(note)
    return "\n".join(notes)
