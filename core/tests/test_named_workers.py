"""Named worker definitions (TD-5133).

``.tst/agents/*.md`` and ``~/.tstdesk/agents/*.md`` define workers with a
name, description, prompt, tool allow list, tier or preset, and policy
rules that can only tighten. The agent cannot write either tree.
``delegate`` runs one by name, the model is offered the list with
descriptions, and ``@name`` in the user's message points the turn at it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from tests.test_delegate import _bind_runtime, _parent_dispatcher, _workspace
from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tests.test_read_tools import make_dispatcher
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier, DecisionRequest
from tstd.autonomy.classifier import is_steering_write
from tstd.config import Preset
from tstd.context.workers import (
    WORKER_ALLOWED_TOOLS,
    load_workers,
    mentioned_workers,
)
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig, PolicyRule, with_worker_rules
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import create_registry
from tstd.tools.boundary import PathGuard, RefusalError
from tstd.tools.delegate import handle_delegate
from tstd.tools.delegate_named import with_named_workers
from tstd.tools.results import HandlerRefusal

READ_ONLY = ("fs_read", "fs_list", "fs_grep", "web_search", "web_fetch")
PROMPT_MARKER = "pelican-orbit-5521"


def _define(root: Path, name: str, front: str, body: str = "") -> Path:
    path = root / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n{body}", encoding="utf-8")
    return path


def _ws_agents(ws: Path) -> Path:
    return ws / ".tst" / "agents"


def _user_agents() -> Path:
    # conftest moves HOME to a throwaway directory for every test.
    return Path.home() / ".tstdesk" / "agents"


def _offered(mock: MockProvider, model: str) -> list[set[str]]:
    return [
        {t.function.name for t in (call.tools or []) if t.function is not None}
        for call in mock.calls
        if call.model == model
    ]


# ── Definitions ─────────────────────────────────────────────────────────


def test_both_trees_define_workers_with_every_field(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _define(
        _ws_agents(ws),
        "reviewer",
        "description: Reviews a diff for bugs\n"
        "tools: [fs_read, fs_grep, shell]\n"
        "tier: worker\n"
        "policy:\n  - tool: shell\n    args: 'git push*'\n    effect: never",
        "Review carefully.\n",
    )
    _define(
        _user_agents(),
        "scout",
        "name: scout\ndescription: Finds things\ntier: brain\npreset: other",
        "Look around.\n",
    )

    catalog = load_workers(ws)

    assert catalog.names == ("reviewer", "scout")
    assert catalog.problems == ()
    reviewer = catalog.get("reviewer")
    assert reviewer is not None
    assert reviewer.description == "Reviews a diff for bugs"
    assert reviewer.prompt == "Review carefully."
    assert reviewer.tools == ("fs_read", "fs_grep", "shell")
    assert reviewer.tier == "worker"
    assert reviewer.source == "workspace"
    assert [(r.tool, r.args, r.effect) for r in reviewer.policy] == [
        ("shell", "git push*", "never")
    ]
    scout = catalog.get("scout")
    assert scout is not None
    assert scout.source == "user"
    assert scout.preset == "other"
    assert scout.tier == "brain"
    # No tools key: the worker is read-only.
    assert scout.tools == READ_ONLY


def test_user_global_wins_on_the_same_name(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _define(_ws_agents(ws), "helper", "description: repo copy\ntools: [shell]")
    _define(_user_agents(), "helper", "description: user copy")
    helper = load_workers(ws).get("helper")
    assert helper is not None
    assert helper.description == "user copy"
    assert helper.tools == READ_ONLY


@pytest.mark.parametrize(
    ("front", "why"),
    [
        ("description: x\npolicy:\n  - tool: shell\n    effect: auto", "only tighten"),
        ("description: x\ntools: [delegate]", "not worker tools"),
        ("description: x\ntools: [github__create_issue]", "not worker tools"),
        ("description: x\nmodel: gpt-5", "does not read: model"),
        ("tools: [fs_read]", "needs a description"),
        ("description: x\ntier: boss", "tier must be"),
        # One kind's tools only: never shell or writes together with the web.
        ("description: x\ntools: [shell, web_fetch]", "one kind's tools"),
        ("description: x\ntools: [fs_write, web_search]", "one kind's tools"),
        # A workspace file cannot pick a dearer tier or another preset.
        ("description: x\ntier: brain", "worker tier of the active preset"),
        ("description: x\ntier: validator", "worker tier of the active preset"),
        ("description: x\npreset: other", "worker tier of the active preset"),
        ("name: 'has space'\ndescription: x", "name must be"),
    ],
)
def test_a_definition_that_would_loosen_or_is_malformed_is_not_loaded(
    tmp_path: Path, front: str, why: str
) -> None:
    ws = _workspace(tmp_path)
    _define(_ws_agents(ws), "bad", front)
    catalog = load_workers(ws)
    assert catalog.workers == ()
    assert len(catalog.problems) == 1
    assert why in catalog.problems[0].reason


def test_allowed_tools_never_include_delegate() -> None:
    assert "delegate" not in WORKER_ALLOWED_TOOLS


def test_worker_rules_can_only_tighten() -> None:
    base = PolicyConfig(rules=[PolicyRule(tool="fs_read", effect="auto")])
    merged = with_worker_rules(
        base,
        [
            PolicyRule(tool="shell", effect="auto"),
            PolicyRule(tool="fs_read", args="secret*", effect="never"),
        ],
    )
    assert [(r.tool, r.effect) for r in merged.rules] == [
        ("fs_read", "auto"),
        ("fs_read", "never"),
    ]


# ── Agent-unwritable ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "relative",
    [Path(".tst/agents/reviewer.md"), Path(".tst/agents"), Path(".TST/AGENTS/x.md")],
)
def test_workspace_definitions_are_class_c_and_refused(tmp_path: Path, relative: Path) -> None:
    target = tmp_path / relative
    b = Boundary(workspace_root=tmp_path, writable_patterns=("**",))
    assert is_steering_write(b, target)
    decision = DecisionClassifier(b).classify(
        DecisionRequest(tool_name="fs_write", writes=(target,), is_mutation=True)
    )
    assert decision.decision_class is DecisionClass.C
    with pytest.raises(RefusalError) as ei:
        PathGuard(b).check_write(target)
    assert ei.value.code == "steering_file"


def test_user_global_definitions_refused_even_inside_the_wall(tmp_path: Path) -> None:
    # The workspace is the home directory, so ~/.tstdesk/agents is inside it.
    home = tmp_path / "home"
    target = home / ".tstdesk" / "agents" / "reviewer.md"
    b = Boundary(workspace_root=home, writable_patterns=("**",))
    assert is_steering_write(b, target)
    with pytest.raises(RefusalError):
        PathGuard(b).check_write(target)


async def test_fs_write_to_a_definition_is_refused(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    dispatcher = make_dispatcher(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = await dispatcher.dispatch(
        "c1",
        "fs_write",
        {"path": ".tst/agents/reviewer.md", "content": "---\ntools: [shell]\n---\n"},
    )
    assert result.status == "error"
    assert result.decision_class is DecisionClass.C
    assert not (tmp_path / ".tst" / "agents" / "reviewer.md").exists()


# ── delegate by name ────────────────────────────────────────────────────


async def test_delegate_runs_the_named_worker_with_its_prompt_tools_and_tier(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    _define(
        _user_agents(),
        "reader",
        "description: Reads notes\ntools: [fs_read, fs_list]\ntier: brain",
        f"Always answer in one line. {PROMPT_MARKER}\n",
    )
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call", tool_name="fs_read", tool_arguments='{"path": "notes.md"}'
                ),
                Script(kind="text", content="notes.md says hello."),
            ]
        }
    )
    _bind_runtime(parent, dispatcher, mock)

    out = await handle_delegate(parent, task="Read notes.md", agent="reader")

    assert out.text == "notes.md says hello."
    # The brain tier ran it, never the worker tier.
    assert {c.model for c in mock.calls} == {"test-brain"}
    # Its allow list exactly: no write, no shell, no consult.
    assert _offered(mock, "test-brain") == [{"fs_read", "fs_list"}] * 2
    system = mock.calls[0].messages[0].content
    assert isinstance(system, str) and PROMPT_MARKER in system
    user = mock.calls[0].messages[1].content
    assert isinstance(user, str) and "named worker 'reader'" in user


async def test_a_named_preset_supplies_the_tier(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _define(_user_agents(), "cheap", "description: cheap reads\npreset: alt")
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(sequences={"alt-worker": [Script(kind="text", content="done")]})
    runtime = _bind_runtime(parent, dispatcher, mock)
    test_preset = runtime.config.presets["test"]
    alt_worker = test_preset.worker.model_copy(update={"slug": "alt-worker"})
    runtime.config.presets["alt"] = Preset(
        brain=test_preset.brain, worker=alt_worker, validator=test_preset.validator
    )

    out = await handle_delegate(parent, task="x", agent="cheap")

    assert out.text == "done"
    assert {c.model for c in mock.calls} == {"alt-worker"}


async def test_an_unconfigured_preset_is_refused(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _define(_user_agents(), "lost", "description: x\npreset: nowhere")
    parent = Session(str(ws))
    _bind_runtime(parent, _parent_dispatcher(ws), MockProvider())
    with pytest.raises(HandlerRefusal) as ei:
        await handle_delegate(parent, task="x", agent="lost")
    assert ei.value.code == "delegate_agent_preset"


async def test_the_workers_policy_tightens_its_calls(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _define(
        _ws_agents(ws),
        "guarded",
        "description: x\ntools: [fs_read]\npolicy:\n  - tool: fs_read\n    effect: never",
    )
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(
                    kind="tool_call", tool_name="fs_read", tool_arguments='{"path": "notes.md"}'
                ),
                Script(kind="text", content="could not read"),
            ]
        }
    )
    _bind_runtime(parent, dispatcher, mock)

    out = await handle_delegate(parent, task="Read notes.md", agent="guarded")

    results = [s for s in out.transcript if s.kind == "tool_result"]
    assert len(results) == 1
    assert results[0].status == "error"
    assert "worker guarded" in results[0].text
    assert "hello from notes" not in json.dumps([s.text for s in out.transcript])
    # The parent's own dispatcher is untouched.
    assert dispatcher.worker_rules == ()


async def test_unknown_worker_and_agent_with_kind_are_refused(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _define(_ws_agents(ws), "reader", "description: x")
    _define(_ws_agents(ws), "broken", "description: x\ntools: [delegate]")
    parent = Session(str(ws))
    _bind_runtime(parent, _parent_dispatcher(ws), MockProvider())
    with pytest.raises(HandlerRefusal) as ei:
        await handle_delegate(parent, task="x", agent="nobody")
    assert ei.value.code == "delegate_agent_unknown"
    assert "reader" in ei.value.message
    with pytest.raises(HandlerRefusal) as ei:
        await handle_delegate(parent, task="x", agent="broken")
    assert "not worker tools" in ei.value.message
    with pytest.raises(HandlerRefusal) as ei:
        await handle_delegate(parent, task="x", agent="reader", kind="worker")
    assert ei.value.code == "delegate_agent_kind"


async def test_plan_mode_refuses_a_worker_that_writes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _define(_ws_agents(ws), "writer", "description: x\ntools: [fs_read, fs_write]")
    parent = Session(str(ws))
    parent.router = TierRouter()
    parent.router.set_plan(True)
    _bind_runtime(parent, _parent_dispatcher(ws), MockProvider())
    with pytest.raises(HandlerRefusal) as ei:
        await handle_delegate(parent, task="x", agent="writer")
    assert ei.value.code == "delegate_agent_plan_mode"


# ── What the model sees, and @name ──────────────────────────────────────


def test_delegate_definition_lists_workers_with_descriptions(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _define(_ws_agents(ws), "reviewer", "description: Reviews a diff for bugs")
    defs = create_registry().to_provider_definitions()
    listed = with_named_workers(defs, load_workers(ws))
    assert listed is not None
    delegate = next(d.function for d in listed if d.function and d.function.name == "delegate")
    assert delegate is not None
    assert "- reviewer: Reviews a diff for bugs" in delegate.description
    assert delegate.parameters["properties"]["agent"]["enum"] == ["reviewer"]
    # No definitions: the request is exactly what it was.
    assert with_named_workers(defs, load_workers(tmp_path / "empty")) is defs


def test_mentions_match_only_defined_workers(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _define(_ws_agents(ws), "reviewer", "description: x")
    catalog = load_workers(ws)
    assert mentioned_workers("@reviewer check this", catalog) == ("reviewer",)
    assert mentioned_workers("ask @reviewer, then @reviewer again", catalog) == ("reviewer",)
    assert mentioned_workers("mail me at a@reviewer.com or @nobody", catalog) == ()


async def test_at_name_in_the_message_targets_that_worker(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _define(
        _ws_agents(ws),
        "reader",
        "description: Reads notes for you\ntools: [fs_read]",
        f"{PROMPT_MARKER}\n",
    )
    session = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="delegate",
                    tool_arguments='{"task": "Read notes.md", "agent": "reader"}',
                ),
                Script(kind="text", content="The reader says hello."),
            ],
            "test-worker": [Script(kind="text", content="notes.md says hello.")],
        }
    )
    runner = await start_loop(
        session,
        TierRouter(),
        mock,
        make_config(),
        registry=dispatcher.registry,
        dispatcher=dispatcher,
    )
    try:
        await session.add_user_message("@reader what is in notes.md?")
        await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    brain = [c for c in mock.calls if c.model == "test-brain"]
    first_user = brain[0].messages[-1].content
    assert isinstance(first_user, str)
    assert first_user.startswith("@reader what is in notes.md?")
    assert 'delegate with agent "reader"' in first_user
    delegate = next(
        t.function for t in (brain[0].tools or []) if t.function and t.function.name == "delegate"
    )
    assert delegate is not None and "- reader: Reads notes for you" in delegate.description
    results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
    assert [r.output for r in results] == ["notes.md says hello."]
    worker = [c for c in mock.calls if c.model == "test-worker"]
    system = worker[0].messages[0].content
    assert isinstance(system, str) and PROMPT_MARKER in system
