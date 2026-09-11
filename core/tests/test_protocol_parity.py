"""The daemon and the UI must agree on the wire, in both directions.

This gap has cost the project three shipped-but-dead features: TD-803's
policy rules, TD-1706's usage panel, and TD-3407's Screen-pane glow.  Each
time a type existed on one side of the wire and not the other, every unit
test stayed green — the stores are tested by calling their reducers
directly, so nothing ever exercised the boundary.

``ui/src/lib/client-event-gate.test.ts`` closes half of it, comparing
``DaemonEventUnion`` against the client's ``KNOWN_EVENT_TYPES``.  Both of
those live in the UI, so a Python event that never reached ``protocol.ts``
at all satisfies it and is still dropped.  This is the other half: the
Python models are the source of truth, and the TypeScript declarations must
match them exactly.

Neither language can enumerate the other's types, so this reads the
declarations out of ``protocol.ts``.  Crude, and the only thing that
watches this seam from the daemon's side.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tstd import protocol

_PROTOCOL_TS = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "protocol.ts"

_EVENT_DECL = re.compile(r'interface \w+ extends DaemonEvent \{\s*type: "([a-z_]+)"')
_MESSAGE_DECL = re.compile(r'interface \w+ extends ClientMessage \{\s*type: "([a-z_]+)"')


def _wire_types(base: type) -> dict[str, str]:
    """Every ``type`` literal reachable from ``base``, mapped to its class."""
    found: dict[str, str] = {}
    stack = list(base.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        field = cls.model_fields.get("type")
        if field is None or field.default is None:
            continue
        found[str(field.default)] = cls.__name__
    return found


def _declared(pattern: re.Pattern[str]) -> set[str]:
    if not _PROTOCOL_TS.is_file():
        pytest.skip("the UI tree is not checked out beside core")
    return set(pattern.findall(_PROTOCOL_TS.read_text(encoding="utf-8")))


def test_the_parse_found_something_so_a_broken_regex_cannot_pass() -> None:
    # Every assertion below is a set comparison, and two empty sets are
    # equal. Without this, a rename in protocol.ts that broke the parse
    # would turn this whole file into a no-op that still reports green.
    assert len(_declared(_EVENT_DECL)) > 15
    assert len(_declared(_MESSAGE_DECL)) > 15
    assert len(_wire_types(protocol.DaemonEvent)) > 15
    assert len(_wire_types(protocol.ClientMessage)) > 15


def test_every_daemon_event_is_declared_in_the_ui() -> None:
    """A Python event absent here is dropped before any store sees it."""
    emitted = _wire_types(protocol.DaemonEvent)
    missing = sorted(set(emitted) - _declared(_EVENT_DECL))
    assert missing == [], (
        f"daemon events with no DaemonEventUnion member: {missing}. "
        "The daemon can send these and the UI will silently drop them."
    )


def test_the_ui_declares_no_event_the_daemon_cannot_send() -> None:
    """The other direction: a stale declaration is a feature that moved."""
    emitted = _wire_types(protocol.DaemonEvent)
    stale = sorted(_declared(_EVENT_DECL) - set(emitted))
    assert stale == [], f"DaemonEventUnion members no daemon event produces: {stale}"


def test_every_client_message_the_ui_can_send_has_a_daemon_model() -> None:
    """A message with no model is rejected at parse, before any handler."""
    accepted = _wire_types(protocol.ClientMessage)
    unknown = sorted(_declared(_MESSAGE_DECL) - set(accepted))
    assert unknown == [], (
        f"client messages the daemon has no model for: {unknown}. "
        "The UI can send these and the daemon will refuse them."
    )


def test_every_daemon_message_model_is_reachable_from_the_ui() -> None:
    accepted = _wire_types(protocol.ClientMessage)
    undeclared = sorted(set(accepted) - _declared(_MESSAGE_DECL))
    assert undeclared == [], f"client messages the UI cannot construct: {undeclared}"
