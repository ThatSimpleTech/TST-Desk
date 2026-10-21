"""TD-5015: the bridged id grammar and the lane's frame rewriting.

The invariant: a frame from another computer that carries a
``session_id`` is never dropped, and every session-id field in it comes
back bridged. The sample of every event type is the protocol fixture
generator's, so a new event joins this test the day it gets a fixture.
"""

from __future__ import annotations

import importlib.util
import json
import typing
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tstd import protocol
from tstd.bridge_wire import (
    _rewrite_ids,
    bridged_id,
    is_bridged,
    is_session_id_key,
    merge_rows,
    remote_rows,
    rewrite_from_remote,
    rewrite_to_remote,
    split_bridged,
    validate_downstream,
)
from tstd.peer_policy import FORWARDED_VERBS
from tstd.protocol import parse_daemon_event

MCH = "mch_0123456789ab"
OTHER = "mch_ba9876543210"
_GENERATOR = Path(__file__).resolve().parent.parent / "scripts" / "generate_protocol_fixtures.py"


def _fixtures() -> dict[str, dict[str, Any]]:
    spec = importlib.util.spec_from_file_location("_protocol_fixtures", _GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name: json.loads(msg.model_dump_json()) for name, msg in module.FIXTURES.items()}


def _event_fixtures() -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for name, frame in _fixtures().items():
        try:
            parse_daemon_event(json.dumps(frame))
        except protocol.HandshakeError:
            continue
        events[name] = frame
    return events


def _union_models(union: Any) -> tuple[type[BaseModel], ...]:
    return typing.get_args(typing.get_args(union)[0])


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        found.extend(_nested_models(arg))
    return found


def _session_fields(model: type[BaseModel], seen: set[type[BaseModel]]) -> list[str]:
    """Every field name at any depth that names a session id."""
    if model in seen:
        return []
    seen.add(model)
    names: list[str] = []
    for name, field in model.model_fields.items():
        if "session" in name and name.endswith(("_id", "_ids")):
            names.append(f"{model.__name__}.{name}")
        for nested in _nested_models(field.annotation):
            names.extend(_session_fields(nested, seen))
    return names


def _session_values(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if is_session_id_key(key) and isinstance(item, str) and item:
                out.append(item)
            else:
                out.extend(_session_values(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_session_values(item))
    return out


# ── the id grammar ───────────────────────────────────────────────────


def test_a_bridged_id_round_trips() -> None:
    raw = str(uuid.uuid4())
    sid = bridged_id(raw, MCH)
    assert sid == f"{raw}:{MCH}"
    assert split_bridged(sid) == (raw, MCH)
    assert is_bridged(sid)


@pytest.mark.parametrize(
    "sid",
    [
        str(uuid.uuid4()),
        "sess-1",
        f"a:b:{MCH}",
        f":{MCH}",
        "abc:mch_0123",
        "abc:mch_0123456789AB",
        f"abc:{MCH}x",
    ],
)
def test_own_and_malformed_ids_never_parse_as_bridged(sid: str) -> None:
    assert split_bridged(sid) is None and not is_bridged(sid)


def test_non_strings_are_never_bridged() -> None:
    assert not is_bridged(None) and not is_bridged(7)


def test_every_session_field_in_every_event_is_one_the_lane_rewrites() -> None:
    """Model introspection: a new ``foo_session`` field would escape the rewrite."""
    seen: set[type[BaseModel]] = set()
    fields = [
        f for model in _union_models(protocol.DaemonEventT) for f in _session_fields(model, seen)
    ]
    assert fields, "the walk found nothing: the union shape changed"
    escaped = [f for f in fields if not is_session_id_key(f.split(".", 1)[1])]
    assert escaped == []
    assert any(f.endswith(".last_session_id") for f in fields)


def test_every_forwarded_verb_but_list_sessions_requires_a_session_id() -> None:
    models = {
        model.model_fields["type"].default: model
        for model in _union_models(protocol.ClientMessageT)
    }
    for verb in FORWARDED_VERBS - {"list_sessions"}:
        field = models[verb].model_fields.get("session_id")
        assert field is not None and field.is_required() and field.annotation is str, verb


# ── up the lane ──────────────────────────────────────────────────────


def test_up_the_lane_only_the_top_level_session_id_changes() -> None:
    frame = {"type": "approve", "session_id": bridged_id("s1", MCH), "tool_call_id": "tc-1"}
    out = rewrite_to_remote(frame)
    assert out == (MCH, {"type": "approve", "session_id": "s1", "tool_call_id": "tc-1"})
    assert frame["session_id"] == bridged_id("s1", MCH)
    assert rewrite_to_remote({"type": "approve", "session_id": "s1"}) is None
    assert rewrite_to_remote({"type": "list_sessions"}) is None


# ── down the lane ────────────────────────────────────────────────────


def test_every_session_scoped_frame_is_forwarded_with_every_id_bridged() -> None:
    """The invariant, over a sample of every event type."""
    events = _event_fixtures()
    assert len(events) > 40
    checked = 0
    for name, frame in events.items():
        if not isinstance(frame.get("session_id"), str) or not frame["session_id"]:
            continue
        out = rewrite_from_remote(frame, MCH, "tower")
        assert out is not None, name
        for value in _session_values(out):
            assert split_bridged(value) is not None and value.endswith(MCH), (name, value)
        assert len(_session_values(out)) == len(_session_values(frame)), name
        checked += 1
    assert checked > 30


def test_machine_wide_frames_are_dropped() -> None:
    kept = {"session_list", "error"}
    for name, frame in _event_fixtures().items():
        if frame.get("session_id") or frame["type"] in kept:
            continue
        assert rewrite_from_remote(frame, MCH, "tower") is None, name


def test_nested_ids_and_list_items_are_rewritten() -> None:
    frame = {
        "type": "job_runs",
        "job_id": "j1",
        "session_id": "s0",
        "runs": [{"session_id": "s1"}, {"session_id": None}, {"parent_session_id": "s2"}],
    }
    # Not a valid job_runs, but session-scoped: passed on whole, every id rewritten.
    assert rewrite_from_remote(frame, MCH, "tower") == _rewrite_ids(frame, MCH)
    rewritten = _rewrite_ids(frame, MCH)
    assert rewritten["session_id"] == bridged_id("s0", MCH)
    assert rewritten["runs"][0]["session_id"] == bridged_id("s1", MCH)
    assert rewritten["runs"][1]["session_id"] is None
    assert rewritten["runs"][2]["parent_session_id"] == bridged_id("s2", MCH)
    assert rewritten["job_id"] == "j1"


def test_an_id_less_error_names_the_computer_and_a_session_error_keeps_its_text() -> None:
    plain = rewrite_from_remote(
        {"type": "error", "code": "x", "message": "Disk full"}, MCH, "tower"
    )
    assert plain == {"type": "error", "code": "x", "message": "On tower: Disk full", "machine": MCH}
    scoped = rewrite_from_remote(
        {"type": "error", "code": "x", "message": "No such tool", "session_id": "s1"}, MCH, "tower"
    )
    assert scoped is not None
    assert scoped["message"] == "No such tool" and scoped["session_id"] == bridged_id("s1", MCH)
    assert scoped["machine"] == MCH


def test_a_second_hop_frame_is_dropped() -> None:
    hop = {"type": "assistant_delta", "session_id": bridged_id("s1", OTHER), "text": "x", "seq": 3}
    assert rewrite_from_remote(hop, MCH, "tower") is None


def test_an_unknown_type_with_a_session_id_passes_and_without_one_drops() -> None:
    newer = {"type": "from_the_future", "session_id": "s1", "seq": 9}
    assert rewrite_from_remote(newer, MCH, "tower") == {
        "type": "from_the_future",
        "session_id": bridged_id("s1", MCH),
        "seq": 9,
    }
    assert rewrite_from_remote({"type": "from_the_future"}, MCH, "tower") is None
    assert validate_downstream(newer) is None


def test_a_malformed_known_frame_with_a_session_id_still_passes() -> None:
    """Version skew: a newer computer's state value must not open a seq gap."""
    newer = {"type": "session_state", "session_id": "s1", "seq": 7, "state": "hibernating"}
    assert validate_downstream(newer) is False
    assert rewrite_from_remote(newer, MCH, "tower") == {
        **newer,
        "session_id": bridged_id("s1", MCH),
    }
    error = {"type": "error", "session_id": "s1", "code": 3, "message": "x"}
    out = rewrite_from_remote(error, MCH, "tower")
    assert out is not None and out["session_id"] == bridged_id("s1", MCH)
    assert out["machine"] == MCH and out["message"] == "x"


def test_a_malformed_frame_without_a_session_id_is_dropped() -> None:
    assert rewrite_from_remote({"type": "error", "code": 3, "message": "x"}, MCH, "tower") is None


def _row(sid: str, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": sid,
        "workspace_path": "/w",
        "state": "idle",
        "created_at": "2026-10-14T00:00:00Z",
        "updated_at": "2026-10-14T00:00:00Z",
        "event_count": 0,
        **extra,
    }


def test_a_session_list_comes_back_bridged_one_hop_and_tagged() -> None:
    """A peer that sends bridged-grammar ids or its own ``machine`` stays one hop."""
    frame = {
        "type": "session_list",
        "sessions": [
            _row("s1", title="one"),
            _row(bridged_id("s2", OTHER), title="hop", machine=OTHER),
            _row("s3", machine="mch_ffffffffffff"),
        ],
    }
    out = rewrite_from_remote(frame, MCH, "tower")
    assert out is not None
    ids = [row["session_id"] for row in out["sessions"]]
    assert ids == [bridged_id("s1", MCH), bridged_id("s3", MCH)]
    assert {row["machine"] for row in out["sessions"]} == {MCH}


def test_a_skewed_session_list_loses_only_its_bad_rows() -> None:
    frame = {
        "type": "session_list",
        "sessions": [_row("s1"), "junk", _row("s2", state="hibernating"), _row("s3")],
    }
    out = rewrite_from_remote(frame, MCH, "tower")
    assert out is not None
    assert [r["session_id"] for r in out["sessions"]] == [
        bridged_id("s1", MCH),
        bridged_id("s3", MCH),
    ]
    junk = rewrite_from_remote({"type": "session_list", "sessions": "junk"}, MCH, "tower")
    assert junk is not None and junk["sessions"] == []


def test_remote_rows_drop_bridged_and_empty_ids() -> None:
    rows = remote_rows([{"session_id": ""}, {"session_id": "a:b"}, {"session_id": "s1"}], MCH)
    assert rows == [{"session_id": bridged_id("s1", MCH), "machine": MCH}]


def test_merge_keeps_own_rows_first_and_unreachable_rows() -> None:
    own = [{"session_id": "s0"}]
    cached = {
        MCH: [{"session_id": bridged_id("s1", MCH), "machine": MCH}],
        OTHER: [{"session_id": bridged_id("s9", OTHER), "machine": OTHER}],
    }
    merged = merge_rows(own, cached)
    assert [row["session_id"] for row in merged] == [
        "s0",
        bridged_id("s1", MCH),
        bridged_id("s9", OTHER),
    ]
    merged[0]["session_id"] = "changed"
    assert own[0]["session_id"] == "s0"


def test_artifact_chunk_passes_with_bridged_id() -> None:
    """TD-5143: the chunk verb goes up with the plain id; the bytes come back as sent."""
    aid = "0" * 32
    up = {"type": "read_artifact_chunk", "session_id": bridged_id("s1", MCH)}
    up |= {"artifact_id": aid, "offset": 0, "length": 524288}
    assert rewrite_to_remote(up) == (MCH, {**up, "session_id": "s1"})
    # Base64 that spells an AWS key id by chance: the hub's re-scrub keeps it.
    data = "QUtJQUFBQUFBQUFBQUFBQUFBQUFB" + "AKIAABCDEFGHIJKLMNOP" + "aGk="
    down = {"type": "artifact_chunk", "seq": 1, "session_id": "s1", "artifact_id": aid}
    down |= {"offset": 0, "size": 2, "sha256": "a" * 64, "data_b64": data, "eof": True}
    down |= {"error_code": None}
    out = rewrite_from_remote(down, MCH, "tower")
    assert out is not None
    assert out["session_id"] == bridged_id("s1", MCH)
    assert out["artifact_id"] == aid
    relayed = json.loads(protocol.scrub_wire_json(json.dumps(out)))
    assert relayed["data_b64"] == data
    assert relayed["session_id"] == bridged_id("s1", MCH)
