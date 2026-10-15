"""TD-5143: path fields that hold a list of paths (security-critical).

Before TD-5143 every path site in dispatch and policy handled strings
only, so a list would have skipped the workspace wall without a sound.
These tests pin that every element is guarded, classified, recorded and
matched against policy rules, that a malformed value fails closed, and
that the string-path tools behave exactly as before.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.test_dispatch import attach_auto_approver, make_classifier
from tstd.autonomy import Boundary, DecisionClass
from tstd.autonomy.classifier import canonical_path
from tstd.policy import (
    PolicyConfig,
    PolicyRule,
    format_summary,
    resolve_explained,
    summarize_arguments,
    summarize_each,
)
from tstd.session import Session
from tstd.tools import Tool, ToolDispatcher, ToolRegistry, build_decision_request
from tstd.tools.boundary import PathGuard
from tstd.tools.registry import create_registry, path_values

_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["files"],
}

READ_MANY = Tool(
    name="read_many",
    description="Reads several paths",
    parameters=_LIST_SCHEMA,
    side_effect_class="auto",
    parallel_safe=True,
    path_fields=("files",),
)

# No ``type`` on the field, so the schema lets anything through and the
# dispatcher's own check is what refuses.
LAX_READ = Tool(
    name="lax_read",
    parameters={"type": "object", "properties": {"files": {}}},
    path_fields=("files",),
)
LAX_WRITE = Tool(
    name="lax_write",
    parameters={"type": "object", "properties": {"path": {}}},
    side_effect_class="ask",
    path_fields=("path",),
    mutates=True,
)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "secrets").mkdir(parents=True)
    for name in ("a.txt", "b.txt", "ok.txt", "secrets/k"):
        (ws / name).write_text(name, encoding="utf-8")
    (tmp_path / "outside.txt").write_text("out", encoding="utf-8")
    return ws


def _dispatcher(
    ws: Path, *tools: Tool, policy: PolicyConfig | None = None
) -> tuple[ToolDispatcher, list[dict[str, Any]]]:
    """A guarded dispatcher whose handlers record the calls they receive."""
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    dispatcher = attach_auto_approver(
        ToolDispatcher(
            registry,
            classifier=make_classifier(str(ws)),
            path_guard=PathGuard(Boundary(workspace_root=ws)),
            workspace=ws,
        )
    )
    if policy is not None:
        dispatcher.policy = policy
    calls: list[dict[str, Any]] = []

    async def _handler(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "ran"

    for tool in tools:
        dispatcher.register_handler(tool.name, _handler)
    return dispatcher, calls


def test_path_values_shapes() -> None:
    assert path_values({}, "files") == []
    assert path_values({"files": None}, "files") == []
    assert path_values({"files": "a.txt"}, "files") == ["a.txt"]
    assert path_values({"files": ["a.txt", "b.txt"]}, "files") == ["a.txt", "b.txt"]
    assert path_values({"files": []}, "files") == []
    assert path_values({"files": ["a.txt", 3]}, "files") is None
    assert path_values({"files": {"a": "b"}}, "files") is None
    assert path_values({"files": 7}, "files") is None
    assert path_values({"files": [["a.txt"]]}, "files") is None


@pytest.mark.parametrize("escape", ["../outside.txt", "OUTSIDE_ABS", "link-out"])
async def test_every_list_element_is_guarded(tmp_path: Path, escape: str) -> None:
    ws = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    if escape == "OUTSIDE_ABS":
        escape = str(outside)
    elif escape == "link-out":
        if not hasattr(os, "symlink"):
            pytest.skip("no symlinks")
        try:
            (ws / "link-out").symlink_to(outside)
        except OSError:
            pytest.skip("symlinks need privileges here")
    dispatcher, calls = _dispatcher(ws, READ_MANY)
    result = await dispatcher.dispatch("c1", "read_many", {"files": ["ok.txt", escape, "a.txt"]})
    assert result.status == "error"
    assert result.error_code == "boundary_refusal"
    assert result.decision_class is DecisionClass.C
    assert result.output.startswith("Refused: path outside the workspace")
    assert calls == []


async def test_in_workspace_list_reaches_the_handler_canonical(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    dispatcher, calls = _dispatcher(ws, READ_MANY)
    result = await dispatcher.dispatch("c1", "read_many", {"files": ["a.txt", str(ws / "b.txt")]})
    assert result.status == "success", result.output
    real = canonical_path(ws)
    assert calls[0]["files"] == [str(real / "a.txt"), str(real / "b.txt")]


@pytest.mark.parametrize("value", [["a.txt", 3], {"path": "a.txt"}, 42, [None]])
async def test_malformed_path_field_is_refused(tmp_path: Path, value: object) -> None:
    ws = _workspace(tmp_path)
    dispatcher, calls = _dispatcher(ws, LAX_READ)
    result = await dispatcher.dispatch("c1", "lax_read", {"files": value})
    assert result.status == "error"
    assert result.error_code == "boundary_refusal"
    assert result.decision_class is DecisionClass.C
    assert result.output == "Refused: files must be a path or a list of paths"
    assert calls == []


async def test_mutating_tool_with_array_path_field_is_refused_at_register_and_dispatch(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="one path per field"):
        registry.register(
            Tool(
                name="write_many",
                parameters=_LIST_SCHEMA,
                path_fields=("files",),
                mutates=True,
            )
        )
    with pytest.raises(ValueError, match="one path per field"):
        registry.register(
            Tool(
                name="write_either",
                parameters={
                    "type": "object",
                    "properties": {"files": {"type": ["string", "array"]}},
                },
                path_fields=("files",),
                mutates=True,
            )
        )
    assert registry.get("write_many") is None and registry.get("write_either") is None
    # The same array on a read tool is fine.
    registry.register(READ_MANY)

    # A schema that does not say "array" still cannot smuggle a list past
    # the write checks: dispatch refuses it before the handler.
    ws = _workspace(tmp_path)
    dispatcher, calls = _dispatcher(ws, LAX_WRITE)
    result = await dispatcher.dispatch("c1", "lax_write", {"path": ["a.txt", "b.txt"]})
    assert result.status == "error"
    assert result.error_code == "boundary_refusal"
    assert result.decision_class is DecisionClass.C
    assert result.output == "Refused: write tools take one path per field"
    assert calls == []
    # One path per field still writes.
    ok = await dispatcher.dispatch("c2", "lax_write", {"path": "a.txt"})
    assert ok.status == "success", ok.output


def test_decision_request_reads_hold_every_element() -> None:
    request = build_decision_request(READ_MANY, {"files": ["/w/a.txt", "/w/b.txt", "/w/c"]})
    assert request.reads == (Path("/w/a.txt"), Path("/w/b.txt"), Path("/w/c"))
    assert request.writes == ()
    assert request.has_path_host_metadata is True
    # Malformed contributes no path; the guard refuses the call later.
    assert build_decision_request(LAX_READ, {"files": {"x": 1}}).reads == ()


async def test_touch_tracking_records_every_element(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    monkeypatch.chdir(ws)
    session = Session(str(ws))
    dispatcher, _calls = _dispatcher(ws, READ_MANY)
    result = await dispatcher.dispatch(
        "c1", "read_many", {"files": ["a.txt", "secrets/k"]}, session=session
    )
    assert result.status == "success", result.output
    assert session.touched_paths == {"a.txt", "secrets/k"}


async def test_policy_never_rule_on_second_element_wins(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    policy = PolicyConfig(rules=[PolicyRule(tool="read_many", args="secrets/*", effect="never")])
    arguments = {"files": [str(ws / "ok.txt"), str(ws / "secrets" / "k")]}
    decision = resolve_explained(policy, READ_MANY, arguments, DecisionClass.A, ws)
    assert decision.effect == "never"
    assert decision.rule is not None and decision.rule.args == "secrets/*"
    # Skip-all never beats a never rule.
    assert (
        resolve_explained(policy, READ_MANY, arguments, DecisionClass.A, ws, skip_all=True).effect
        == "never"
    )

    dispatcher, calls = _dispatcher(ws, READ_MANY, policy=policy)
    result = await dispatcher.dispatch("c1", "read_many", {"files": ["ok.txt", "secrets/k"]})
    assert result.status == "error"
    assert result.error_code == "policy_denied"
    assert calls == []


def test_policy_most_restrictive_across_elements_then_skip_all(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    policy = PolicyConfig(
        rules=[
            PolicyRule(tool="read_many", args="a.txt", effect="auto"),
            PolicyRule(tool="read_many", args="b.txt", effect="ask"),
        ]
    )
    both = {"files": [str(ws / "a.txt"), str(ws / "b.txt")]}
    decision = resolve_explained(policy, READ_MANY, both, DecisionClass.A, ws)
    assert decision.effect == "ask"
    assert decision.rule is not None and decision.rule.args == "b.txt"
    # The first decision with the highest restrictiveness stands.
    flipped = {"files": [str(ws / "b.txt"), str(ws / "a.txt")]}
    assert resolve_explained(policy, READ_MANY, flipped, DecisionClass.A, ws).rule == decision.rule
    # Skip-all runs once afterwards and takes the ask.
    skipped = resolve_explained(policy, READ_MANY, both, DecisionClass.A, ws, skip_all=True)
    assert skipped.effect == "auto" and skipped.reason == "skip-all approvals is on"
    # Every element auto: auto.
    only_a = {"files": [str(ws / "a.txt"), str(ws / "a.txt")]}
    assert resolve_explained(policy, READ_MANY, only_a, DecisionClass.A, ws).effect == "auto"
    # The Class C wall still runs once: an auto rule never auto-runs C.
    walled = resolve_explained(policy, READ_MANY, only_a, DecisionClass.C, ws)
    assert walled.effect == "ask"
    assert walled.reason.endswith("decision class C may not run automatically")


def test_format_summary_shows_plus_n_more(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    one = {"files": [str(ws / "a.txt")]}
    three = {"files": [str(ws / "a.txt"), str(ws / "b.txt"), str(ws / "secrets" / "k")]}
    assert format_summary(READ_MANY, one, ws) == "Read a.txt"
    assert format_summary(READ_MANY, three, ws) == "Read a.txt (+2 more)"
    assert summarize_each(READ_MANY, three, ws) == ["a.txt", "b.txt", "secrets/k"]
    # "Always allow" scopes to the first path, as for a single path.
    assert summarize_arguments(READ_MANY, three, ws) == "a.txt"


# Tools whose path fields are strings: every builtin except send_file, whose
# ``files`` is the one array field (covered above and in test_send_file.py).
_STRING_PATH_TOOLS = sorted(
    t.name
    for t in create_registry().list_tools()
    if t.path_fields
    and all(t.parameters["properties"][f].get("type") != "array" for f in t.path_fields)
)


@pytest.mark.parametrize("name", _STRING_PATH_TOOLS)
async def test_existing_string_tools_unchanged(tmp_path: Path, name: str) -> None:
    assert {"fs_read", "fs_list", "fs_write", "fs_edit"} <= set(_STRING_PATH_TOOLS)
    tool = create_registry().require(name)
    ws = _workspace(tmp_path)
    field = tool.path_fields[0]
    arguments = {field: str(ws / "a.txt")}
    assert path_values(arguments, field) == [str(ws / "a.txt")]
    request = build_decision_request(tool, arguments)
    expected = (Path(ws / "a.txt"),)
    assert (request.writes if tool.mutates else request.reads) == expected
    assert summarize_each(tool, arguments, ws) == ["a.txt"]
    assert summarize_arguments(tool, arguments, ws) == "a.txt"
    verb = "Write" if tool.mutates else "Read"
    assert format_summary(tool, arguments, ws) == f"{verb} a.txt"

    # The wall still refuses an outside string path, before the handler.
    dispatcher, calls = _dispatcher(ws, tool)
    result = await dispatcher.dispatch("c1", name, {**_required(tool), field: "../outside.txt"})
    assert result.error_code == "boundary_refusal"
    assert result.decision_class is DecisionClass.C
    assert calls == []


def _required(tool: Tool) -> dict[str, Any]:
    """Placeholder values for the tool's other required string arguments."""
    props: dict[str, Any] = tool.parameters["properties"]
    return {
        key: "x"
        for key in tool.parameters.get("required", [])
        if props.get(key, {}).get("type") == "string"
    }
