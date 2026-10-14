"""Bridged session ids and the lane's frame rewriting (TD-5015). Pure.

A session on a paired computer shows here as ``<raw>:<mch>``: its id on
that computer, a colon, and this hub's handle for the computer
(``mch_`` + 12 hex). A uuid4 raw id has no colon, so own ids never parse
as bridged. The grammar is protocol (docs/architecture.md).

Up the lane only the top-level ``session_id`` changes. Down the lane
every session-id field changes (``session_id`` and any ``*_session_id``,
at any depth, list items included), the frame is validated first, and
the invariant holds: a frame that carries a ``session_id`` is never
dropped. Frames without one are machine-wide state for the other
computer (its jobs, its settings, its pairing) and are dropped, except
``session_list`` (merged by the hub) and ``error`` (named for the
computer it came from).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .logging import get_logger
from .protocol import (
    HandshakeError,
    UnknownMessageTypeError,
    parse_daemon_event,
)

log = get_logger("tstd.bridge_wire")

BRIDGED_ID_RE = re.compile(r"^(?P<raw>[^:]+):(?P<mch>mch_[0-9a-f]{12})$")
MCH_RE = re.compile(r"^mch_[0-9a-f]{12}$")

# Frames without a session id that still cross a lane.
_KEPT_WITHOUT_ID = frozenset({"session_list", "error"})


def bridged_id(raw: str, mch: str) -> str:
    """``<raw>:<mch>``."""
    return f"{raw}:{mch}"


def split_bridged(session_id: str) -> tuple[str, str] | None:
    """``(raw, mch)`` for a bridged id, else None."""
    match = BRIDGED_ID_RE.match(session_id)
    if match is None:
        return None
    return match.group("raw"), match.group("mch")


def is_bridged(session_id: object) -> bool:
    return isinstance(session_id, str) and BRIDGED_ID_RE.match(session_id) is not None


def is_session_id_key(key: str) -> bool:
    """The field names the lane rewrites: ``session_id`` and ``*_session_id``."""
    return key == "session_id" or key.endswith("_session_id")


def rewrite_to_remote(frame: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """``(mch, frame)`` with the top-level ``session_id`` made raw, else None.

    None when the frame's ``session_id`` is missing or not bridged.
    """
    sid = frame.get("session_id")
    if not isinstance(sid, str):
        return None
    parts = split_bridged(sid)
    if parts is None:
        return None
    raw, mch = parts
    out = dict(frame)
    out["session_id"] = raw
    return mch, out


def _rewrite_ids(value: Any, mch: str) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if is_session_id_key(key) and isinstance(item, str) and item:
                out[key] = bridged_id(item, mch)
            else:
                out[key] = _rewrite_ids(item, mch)
        return out
    if isinstance(value, list):
        return [_rewrite_ids(item, mch) for item in value]
    return value


def remote_rows(sessions: Iterable[Any], mch: str) -> list[dict[str, Any]]:
    """Another computer's own rows, as this hub shows them.

    Rows whose id is already bridged are dropped (that computer would be
    a second hop), any upstream ``machine`` is replaced by *mch*, and
    each id becomes ``<raw>:<mch>``.
    """
    rows: list[dict[str, Any]] = []
    for row in sessions:
        if not isinstance(row, dict):
            continue
        sid = row.get("session_id")
        if not isinstance(sid, str) or not sid or ":" in sid:
            continue
        out = dict(row)
        out["session_id"] = bridged_id(sid, mch)
        out["machine"] = mch
        rows.append(out)
    return rows


def _probe(frame: dict[str, Any]) -> dict[str, Any]:
    # ``build_error`` and other direct replies omit seq; the models need one.
    if "seq" in frame:
        return frame
    probe = dict(frame)
    probe["seq"] = 1
    return probe


def validate_downstream(frame: dict[str, Any]) -> bool | None:
    """True for a known, well-formed event; None for an unknown type; False if malformed."""
    try:
        parse_daemon_event(json.dumps(_probe(frame)))
    except UnknownMessageTypeError:
        return None
    except HandshakeError:
        return False
    return True


def rewrite_from_remote(frame: dict[str, Any], mch: str, name: str) -> dict[str, Any] | None:
    """One downstream frame as the viewer sees it, or None to drop it.

    A ``session_list`` comes back with its rows made bridged (the hub
    merges it). An ``error`` gets ``machine``; without a session id its
    message also names the computer. A frame for a session id with a
    colon (already bridged: a second hop) is dropped. An unknown type
    with a session id (a newer computer) still passes, so a lane never
    opens a seq gap; a malformed known type is dropped and logged.
    """
    kind = frame.get("type")
    if not isinstance(kind, str):
        return None
    sid = frame.get("session_id")
    has_id = isinstance(sid, str) and bool(sid)
    if has_id and ":" in str(sid):
        # Bridged already (a second hop), or an id the grammar cannot carry.
        log.warning(
            "dropped a second-hop frame", extra={"extra_fields": {"machine": mch, "type": kind}}
        )
        return None
    if not has_id and kind not in _KEPT_WITHOUT_ID:
        return None
    valid = validate_downstream(frame)
    if valid is False or (valid is None and not has_id):
        log.warning(
            "dropped a malformed frame from another computer",
            extra={"extra_fields": {"machine": mch, "type": kind}},
        )
        return None
    if kind == "session_list":
        out = {key: value for key, value in frame.items() if key != "sessions"}
        out["sessions"] = remote_rows(frame.get("sessions") or [], mch)
        return out
    out = _rewrite_ids(frame, mch)
    if kind == "error":
        out["machine"] = mch
        if not has_id:
            out["message"] = f"On {name}: {frame.get('message', '')}"
    return dict(out)


def merge_rows(
    own: Iterable[dict[str, Any]], machines: Mapping[str, Iterable[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Own rows first, then each computer's cached rows in the given order.

    Unreachable computers keep their last-known rows, so a bound row is
    never lost from the list.
    """
    merged = [dict(row) for row in own]
    for rows in machines.values():
        merged.extend(dict(row) for row in rows)
    return merged
