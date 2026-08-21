"""The architecture guide is checked against the architecture (TD-1504).

An architecture guide rots faster than any other doc, because the things
it describes — the protocol surface, the state machine, the extension
seams — are exactly the things a feature touches.  A hand-maintained list
of protocol messages is stale the first week someone adds one.  So
nothing in ``docs/architecture.md`` is taken on trust here:

* **The protocol tables are derived, not asserted.**  Both tables are
  compared against the discriminated unions in ``tstd.protocol``, in both
  directions, so a new message fails the suite until it is documented and
  a deleted one fails until it is removed.  The per-row metadata (does a
  client message carry a ``session_id``; is an event's seq session- or
  connection-scoped) is read out of the models too, so a message that
  changes shape fails here as well.
* **The state machine is derived** from ``Session.VALID_TRANSITIONS``.
* **The extension seams are imported.**  Every seam the guide names must
  resolve to a real symbol at the path the guide gives for it.
* **The "how to add a tool" walkthrough is executed.**  Its blocks are
  run against a real ``ToolRegistry`` and a real ``ToolDispatcher`` — the
  same chokepoint a shipped tool goes through — and the result is
  compared to the output the guide promises.  A walkthrough nobody runs
  rots within a month.

The one thing checked by prose rather than by structure is ownership
(prime directive §2.5).  ``test_a_session_outlives_its_viewer`` proves it
end to end against a real daemon over a real socket, and the guide quotes
``SessionRunner``'s own docstring so rewording the guarantee fails here.
"""

from __future__ import annotations

import asyncio
import json
import re
from importlib import import_module
from inspect import cleandoc
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, get_args

import pytest
from websockets.asyncio.client import connect

from tstd.autonomy.classifier import Boundary, DecisionClassifier
from tstd.autonomy.worker import AmbiguousClassifier
from tstd.daemon import Daemon
from tstd.policy import ApprovalOutcome, PolicyConfig
from tstd.protocol import (
    PROTOCOL_VERSION,
    ClientMessageT,
    DaemonEvent,
    DaemonEventT,
)
from tstd.session import Session, SessionRunner
from tstd.tools.boundary import PathGuard
from tstd.tools.dispatch import ToolDispatcher
from tstd.tools.registry import Tool, create_registry

ROOT = Path(__file__).resolve().parent.parent.parent
DOC = ROOT / "docs" / "architecture.md"
README = ROOT / "README.md"

_MARKER = re.compile(r"^<!--\s*verify:\s*(?P<kind>[a-z-]+)(?:\s+(?P<arg>\S+))?\s*-->\s*$")
_FENCE = re.compile(r"^```\w*\s*$")
#: A table row's leading `identifier` cell, plus the cells after it.
_ROW = re.compile(r"^\|\s*`(?P<key>[^`]+)`\s*\|(?P<rest>.*)\|\s*$")

#: Stand-in for the temporary path the guide cannot know in advance.
_WORKSPACE = "<workspace>"


# ── Parsing the guide ────────────────────────────────────────────────────


class Block:
    """One ``verify:`` marker and the fenced block or table after it."""

    def __init__(self, kind: str, arg: str | None, body: str, line: int) -> None:
        self.kind = kind
        self.arg = arg
        self.body = body
        self.line = line

    def __repr__(self) -> str:  # pragma: no cover - pytest ids only
        return f"{self.kind}@L{self.line}"


def _doc_text() -> str:
    assert DOC.exists(), f"{DOC} is missing"
    return DOC.read_text(encoding="utf-8")


def _parse(text: str) -> dict[str, Block]:
    """Collect every marked region, keyed by marker kind.

    A marker claims whichever comes first after it: a fenced code block,
    or a markdown table.  Kinds are unique — the guide has one table per
    subject — so a duplicate marker is an error rather than a silent
    overwrite.
    """
    lines = text.split("\n")
    blocks: dict[str, Block] = {}
    index = 0
    while index < len(lines):
        marker = _MARKER.match(lines[index])
        if marker is None:
            index += 1
            continue

        kind, arg = marker.group("kind"), marker.group("arg")
        assert kind not in blocks, f"{DOC}:{index + 1}: duplicate `{kind}` marker"
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        assert cursor < len(lines), f"{DOC}:{index + 1}: `{kind}` marker governs nothing"

        if _FENCE.match(lines[cursor]):
            close = cursor + 1
            while close < len(lines) and lines[close].strip() != "```":
                close += 1
            assert close < len(lines), f"{DOC}:{cursor + 1}: unterminated block"
            body = "\n".join(lines[cursor + 1 : close])
            index = close + 1
        else:
            assert lines[cursor].startswith("|"), (
                f"{DOC}:{cursor + 1}: `{kind}` must be followed by a code block or a table"
            )
            close = cursor
            while close < len(lines) and lines[close].startswith("|"):
                close += 1
            body = "\n".join(lines[cursor:close])
            index = close

        blocks[kind] = Block(kind, arg, body, cursor + 1)
    return blocks


BLOCKS = _parse(_doc_text())


def _require(kind: str) -> Block:
    block = BLOCKS.get(kind)
    assert block is not None, f"{DOC} has no `verify: {kind}` region"
    return block


def _rows(kind: str) -> dict[str, list[str]]:
    """A marked table as ``{first cell: [remaining cells]}``.

    The header and its separator carry no backticked key, so they fall
    out without needing to be counted.
    """
    parsed: dict[str, list[str]] = {}
    for line in _require(kind).body.split("\n"):
        match = _ROW.match(line.rstrip())
        if match is None:
            continue
        cells = [cell.strip() for cell in match.group("rest").split("|")]
        key = match.group("key")
        assert key not in parsed, f"{DOC}: duplicate `{kind}` row for {key}"
        parsed[key] = cells
    assert parsed, f"{DOC}: the `{kind}` table has no rows"
    return parsed


# ── The protocol, derived from the unions ────────────────────────────────


def _members(union: Any) -> dict[str, type[Any]]:
    """Every model in a discriminated union, keyed by its wire ``type``.

    Read off the annotation rather than a hand-kept list: that is the
    whole point — the union is the contract, so it is the thing the guide
    is compared against.
    """
    annotated = get_args(union)[0]
    members: dict[str, type[Any]] = {}
    for model in get_args(annotated):
        wire_type = get_args(model.model_fields["type"].annotation)[0]
        members[wire_type] = model
    return members


CLIENT_MESSAGES = _members(ClientMessageT)
DAEMON_EVENTS = _members(DaemonEventT)


def test_every_client_message_is_documented() -> None:
    """Criterion: the protocol is documented, and stays documented.

    Compared both ways.  Adding a message to ``ClientMessageT`` without a
    table row fails here, which is the only way a protocol reference
    stays complete without somebody auditing it by hand.
    """
    documented = set(_rows("client-messages"))
    assert documented == set(CLIENT_MESSAGES), (
        f"undocumented client messages: {sorted(set(CLIENT_MESSAGES) - documented)}; "
        f"documented but not in ClientMessageT: {sorted(documented - set(CLIENT_MESSAGES))}"
    )


def test_every_daemon_event_is_documented() -> None:
    """The same, for daemon→client events."""
    documented = set(_rows("daemon-events"))
    assert documented == set(DAEMON_EVENTS), (
        f"undocumented daemon events: {sorted(set(DAEMON_EVENTS) - documented)}; "
        f"documented but not in DaemonEventT: {sorted(documented - set(DAEMON_EVENTS))}"
    )


def test_documented_rows_say_something() -> None:
    """A row with an empty purpose cell documents nothing."""
    for kind in ("client-messages", "daemon-events"):
        for key, cells in _rows(kind).items():
            assert cells[-1].strip(), f"{DOC}: `{key}` has an empty purpose cell"


def test_client_message_session_scoping_matches_the_models() -> None:
    """The `Session` column is read out of the models, not asserted.

    A message that gains or loses ``session_id`` — a real change in how a
    client addresses it — fails here until the table catches up.
    """
    wrong = {
        wire_type: cells[0]
        for wire_type, cells in _rows("client-messages").items()
        if cells[0] != ("yes" if "session_id" in CLIENT_MESSAGES[wire_type].model_fields else "—")
    }
    assert not wrong, f"wrong `Session` column for: {wrong}"


def _seq_scope(model: type[Any]) -> str:
    """Where an event's seq comes from, per the model's own declaration.

    A connection-scoped event pins ``seq`` at 1 because it belongs to no
    session's log; a session-scoped one leaves it required and is stamped
    on insertion.  ``ping`` is not a ``DaemonEvent`` at all and has none.
    """
    if not issubclass(model, DaemonEvent):
        return "—"
    return "connection" if model.model_fields["seq"].default == 1 else "session"


def test_event_seq_scoping_matches_the_models() -> None:
    """The `Seq` column, likewise derived.

    This is the column most likely to be got wrong by hand and the one
    that breaks replay when it is: an event that wrongly advances a
    client's sequence bookkeeping corrupts every later ``attach``.
    """
    wrong = {
        wire_type: cells[0]
        for wire_type, cells in _rows("daemon-events").items()
        if cells[0] != _seq_scope(DAEMON_EVENTS[wire_type])
    }
    assert not wrong, f"wrong `Seq` column for: {wrong}"


# ── Why the session owns the loop ────────────────────────────────────────


def test_the_ownership_guarantee_is_quoted_verbatim() -> None:
    """Criterion 1: the guide states it in the runner's own words.

    Quoting the docstring rather than paraphrasing it means rewording the
    guarantee in the code fails here first, instead of leaving the guide
    describing an invariant nobody kept.
    """
    quoted = _require("docstring").body.strip()
    assert _require("docstring").arg == "tstd.session:SessionRunner"
    source = cleandoc(SessionRunner.__doc__ or "")
    assert quoted in source, (
        "the guide does not quote SessionRunner's docstring verbatim; it says:\n"
        f"{quoted}\n\nthe code says:\n{source}"
    )


def test_the_state_machine_matches_the_transition_table() -> None:
    """The documented state machine is `Session.VALID_TRANSITIONS`.

    Adding a state, or an edge between two existing ones, fails here.
    Terminal states are written as *(terminal)* and must really have no
    outgoing edges — ``TERMINAL_STATES`` is derived from this same table,
    so a mislabelled row would misdescribe what can be deleted or moved.
    """
    documented: dict[str, set[str]] = {}
    for state, cells in _rows("states").items():
        target = cells[0]
        if target == "*(terminal)*":
            documented[state] = set()
        else:
            documented[state] = {t.strip().strip("`") for t in target.split(",")}
    assert documented == Session.VALID_TRANSITIONS, (
        f"documented: {documented}\nactual: {Session.VALID_TRANSITIONS}"
    )


async def _wait_for_port(daemon: Daemon) -> int:
    for _ in range(100):
        if daemon.ws_server.port:
            return int(daemon.ws_server.port)
        await asyncio.sleep(0.05)
    raise AssertionError("daemon never bound a port")


@pytest.mark.asyncio
async def test_a_session_outlives_its_viewer(tmp_path: Path) -> None:
    """Prime directive §2.5, proved rather than asserted.

    The guide claims a dropped connection cleans up that connection's
    stream and nothing else.  This opens a real session over a real
    socket, attaches, drops the socket, and checks the runner is still
    running and the event log still accepts events — the two facts that
    would be false if the socket owned the loop.
    """
    with TemporaryDirectory() as tmp:
        daemon = Daemon(data_dir=Path(tmp))
        daemon_task = asyncio.create_task(daemon.run())
        try:
            port = await _wait_for_port(daemon)
            ws = await connect(f"ws://127.0.0.1:{port}")
            hello = {
                "type": "hello",
                "token": daemon.ws_server.token,
                "version": PROTOCOL_VERSION,
            }
            await ws.send(json.dumps(hello))
            assert json.loads(await ws.recv())["type"] == "hello_ack"

            await ws.send(json.dumps({"type": "open_workspace", "path": str(tmp_path)}))
            opened = json.loads(await ws.recv())
            session_id = opened["session_id"]
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))

            session = daemon.session_registry.get(session_id)
            runner = daemon.session_registry.get_runner(session_id)
            assert session is not None and runner is not None
            assert runner.is_running

            seq_before = session.event_log.last_seq
            await ws.close()
            await asyncio.sleep(0.2)  # let the disconnect callback run

            assert runner.is_running, "closing the viewer stopped the session loop"
            assert session.state == "running", f"session fell out of running: {session.state}"
            await session.add_user_message("still here")
            assert session.event_log.last_seq >= seq_before, (
                "the event log stopped accepting events"
            )
        finally:
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)


# ── Extension points ─────────────────────────────────────────────────────


def _resolve(where: str, symbol: str) -> Any:
    """Import the module the guide points at and walk to *symbol*."""
    module_path = where.strip("`").removeprefix("core/").removesuffix(".py").replace("/", ".")
    obj: Any = import_module(module_path)
    for part in symbol.split("."):
        obj = getattr(obj, part)
    return obj


def test_every_named_seam_exists_where_the_guide_says() -> None:
    """Criterion 3: extension points are named — and are real.

    Naming a seam that has been renamed or moved is worse than naming
    none: a contributor follows the path, finds nothing, and concludes
    the guide is fiction.  Each row is resolved through a real import.
    """
    for symbol, cells in _rows("seams").items():
        try:
            assert _resolve(cells[0], symbol) is not None
        except (ImportError, AttributeError) as e:
            raise AssertionError(
                f"{DOC}: seam `{symbol}` at {cells[0]} does not resolve: {e}"
            ) from e


def test_the_seams_the_story_asks_for_are_named() -> None:
    """The story names the seams a contributor most needs; check them off."""
    named = set(_rows("seams"))
    required = {"create_registry", "register_builtin_handlers", "RULE_TABLE", "Precedence"}
    assert required <= named, f"unnamed extension points: {sorted(required - named)}"


def test_the_provider_factory_seam_is_a_closure_in_attach_session_runtime() -> None:
    """The guide calls the provider a closure built in ``_attach_session_runtime``.

    Worth pinning: it is why a session can be opened and attached with no
    API key stored, and turning it into an eagerly-constructed object
    would break first-run without failing any existing test. Open and
    revive share this helper, so the factory is not inside ``_start_session``.
    """
    source = (ROOT / "core" / "tstd" / "daemon.py").read_text(encoding="utf-8")
    start = source.index("async def _attach_session_runtime")
    body = source[start : source.index("\n    async def ", start + 1)]
    assert "async def get_provider()" in body, (
        "the provider factory closure has moved or changed shape"
    )
    assert "_attach_session_runtime" in _rows("seams")["Daemon"][-1]


# ── The "how to add a tool" walkthrough, executed ────────────────────────


def _exec(block: Block, namespace: dict[str, Any]) -> None:
    """Run one of the walkthrough's python blocks in *namespace*."""
    exec(compile(block.body, f"{DOC}:{block.line}", "exec"), namespace)


async def _stub_worker(_prompt: str) -> str:
    """Stand in for the worker-tier classifier, so no model call is made."""
    return "A"


@pytest.mark.asyncio
async def test_the_walkthrough_tool_registers_and_dispatches(tmp_path: Path) -> None:
    """Criterion 4: the walkthrough works, start to finish.

    Every block the guide shows a contributor is executed here against
    the real registry and the real dispatcher — including the classifier,
    the path guard, and the policy gate — and the output is compared with
    the result the guide promises.  A walkthrough that stopped working,
    or a promised result that drifted, fails the suite.
    """
    (tmp_path / "notes.md").write_text(_require("tool-fixture").body + "\n", encoding="utf-8")
    assert _require("tool-fixture").arg == "notes.md"

    # The two blocks a contributor pastes into registry.py and handlers.py,
    # run against the same imports those modules already have.
    registry = create_registry()
    namespace: dict[str, Any] = {
        "registry": registry,
        "Tool": Tool,
        "asyncio": asyncio,
        "Path": Path,
    }
    _exec(_require("tool-registration"), namespace)
    _exec(_require("tool-handler"), namespace)

    assert registry.get("word_count") is not None, "the registration block registered no tool"

    boundary = Boundary(workspace_root=tmp_path)
    dispatcher = ToolDispatcher(
        registry,
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(boundary), call_worker=_stub_worker
        ),
        path_guard=PathGuard(boundary),
        policy=PolicyConfig(),
        approval_handler=lambda *_args: _approved(),
        workspace=tmp_path,
    )
    _exec(_require("tool-wiring"), {**namespace, "dispatcher": dispatcher})

    arguments = json.loads(_require("tool-arguments").body)
    arguments["path"] = arguments["path"].replace(_WORKSPACE, str(tmp_path))

    result = await dispatcher.dispatch("call_1", "word_count", arguments, session=None)
    assert result.status == "success", f"the walkthrough tool failed: {result.output}"
    assert result.output == _require("tool-result").body.strip(), (
        f"the guide promises {_require('tool-result').body.strip()!r}, "
        f"the dispatcher returned {result.output!r}"
    )
    # The teaching point of step 3: a contributor's tool inherits the
    # chokepoint.  An unclassified result here would mean it did not.
    assert result.decision_class is not None, "the walkthrough tool bypassed the classifier"


async def _approved() -> ApprovalOutcome:
    return ApprovalOutcome(True, "")


@pytest.mark.asyncio
async def test_the_walkthrough_tool_is_still_inside_the_boundary(tmp_path: Path) -> None:
    """The guide tells contributors not to re-validate paths themselves.

    That advice is only safe while the dispatcher really does refuse an
    out-of-workspace path for a tool that merely declared ``path_fields``.
    """
    registry = create_registry()
    namespace: dict[str, Any] = {
        "registry": registry,
        "Tool": Tool,
        "asyncio": asyncio,
        "Path": Path,
    }
    _exec(_require("tool-registration"), namespace)
    _exec(_require("tool-handler"), namespace)

    boundary = Boundary(workspace_root=tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")

    dispatcher = ToolDispatcher(
        registry,
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(boundary), call_worker=_stub_worker
        ),
        path_guard=PathGuard(boundary),
        policy=PolicyConfig(),
        approval_handler=lambda *_args: _approved(),
        workspace=tmp_path / "workspace",
    )
    _exec(_require("tool-wiring"), {**namespace, "dispatcher": dispatcher})

    result = await dispatcher.dispatch("call_1", "word_count", {"path": str(outside)}, session=None)
    assert result.status == "error"
    assert result.error_code == "boundary_refusal", (
        f"a path outside the workspace was not refused: {result.output}"
    )


# ── The guide is findable ────────────────────────────────────────────────


def test_the_guide_is_reachable_from_the_readme() -> None:
    """A guide nobody can find is a guide nobody reads."""
    assert "docs/architecture.md" in README.read_text(encoding="utf-8"), (
        "README.md does not link the architecture guide"
    )
