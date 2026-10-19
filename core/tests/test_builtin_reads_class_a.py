"""Built-in reads inside the wall are static Class A (TD-5141).

Adam, 2026-10-14: the shipped read tools are pre-approved, ``.env``
included.  fs_read, fs_list and fs_grep inside the workspace classify A
from the rule table with no worker call; a user policy rule (ask or
never) still decides; outside the wall nothing changes.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClass,
    DecisionClassifier,
)
from tstd.autonomy.classifier import RULE_TABLE
from tstd.policy import ApprovalOutcome, PolicyConfig, PolicyRule, resolve_explained
from tstd.tools import build_decision_request, create_registry, register_builtin_handlers
from tstd.tools.boundary import PathGuard
from tstd.tools.dispatch import ToolDispatcher

READS = {
    "fs_read": {"path": "src/a.py"},
    "fs_list": {"path": "src", "pattern": "**/*.py", "recursive": True},
    "fs_grep": {"pattern": "needle", "path": "."},
}


class SpyWorker:
    """Records every worker call; answers B so a stray call would ask."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return "B"


def _tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("needle = 1\n")
    (root / ".env").write_text("TOKEN=needle\n")


def _request(root: Path, name: str, arguments: dict[str, object]):
    tool = create_registry().get(name)
    assert tool is not None
    guard = PathGuard(Boundary(workspace_root=root))
    args = dict(arguments)
    for field in tool.path_fields:
        if isinstance(args.get(field), str):
            args[field] = str(guard.canonicalize(args[field]))
    return build_decision_request(tool, args)


class _Harness:
    def __init__(self, root: Path, policy: PolicyConfig | None = None) -> None:
        self.worker = SpyWorker()
        self.audits: list[object] = []
        self.asked: list[tuple[str, str]] = []
        boundary = Boundary(workspace_root=root)
        self.dispatcher = ToolDispatcher(
            create_registry(),
            classifier=AmbiguousClassifier(
                static=DecisionClassifier(boundary),
                call_worker=self.worker,
                on_audit=self.audits.append,
            ),
            path_guard=PathGuard(boundary),
            policy=policy if policy is not None else PolicyConfig(),
            workspace=root,
        )
        register_builtin_handlers(self.dispatcher)

        async def _approve(_id, tool, arguments, _cls, summary, reason) -> ApprovalOutcome:
            self.asked.append((tool.name, reason))
            return ApprovalOutcome(True)

        self.dispatcher.approval_handler = _approve


# ── The rule table ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(READS))
def test_each_builtin_read_is_static_a_inside_the_wall(tmp_path: Path, name: str) -> None:
    _tree(tmp_path)
    decision = DecisionClassifier(Boundary(workspace_root=tmp_path)).classify(
        _request(tmp_path, name, READS[name])
    )
    assert decision.decision_class is DecisionClass.A
    assert decision.rule is not None and decision.rule.id == "builtin-read"
    assert decision.reason.startswith("[builtin-read]")


def test_env_file_read_is_class_a(tmp_path: Path) -> None:
    """Adam's call: .env is not special-cased by the table."""
    _tree(tmp_path)
    decision = DecisionClassifier(Boundary(workspace_root=tmp_path)).classify(
        _request(tmp_path, "fs_read", {"path": ".env"})
    )
    assert decision.decision_class is DecisionClass.A


@pytest.mark.parametrize("name", sorted(READS))
def test_outside_the_wall_stays_c(tmp_path: Path, name: str) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    args = dict(READS[name], path=str(tmp_path))
    decision = DecisionClassifier(Boundary(workspace_root=ws)).classify(_request(ws, name, args))
    assert decision.decision_class is DecisionClass.C
    assert decision.rule is not None and decision.rule.id == "path-outside-workspace"


def test_dotdot_escape_stays_c(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.txt").write_text("x\n")
    decision = DecisionClassifier(Boundary(workspace_root=ws)).classify(
        _request(ws, "fs_read", {"path": "../secret.txt"})
    )
    assert decision.decision_class is DecisionClass.C


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_out_of_the_wall_stays_c(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "key.pem").write_text("secret\n")
    (ws / "link").symlink_to(outside, target_is_directory=True)
    classifier = DecisionClassifier(Boundary(workspace_root=ws))
    for name, args in (
        ("fs_read", {"path": "link/key.pem"}),
        ("fs_list", {"path": "link"}),
        ("fs_grep", {"pattern": "secret", "path": "link"}),
    ):
        # Not canonicalized first: the table resolves symlinks itself.
        tool = create_registry().get(name)
        request = build_decision_request(tool, {**args, "path": str(ws / args["path"])})
        assert classifier.classify(request).decision_class is DecisionClass.C, name


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_inside_the_wall_is_a(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "alias.py").symlink_to(tmp_path / "src" / "a.py")
    decision = DecisionClassifier(Boundary(workspace_root=tmp_path)).classify(
        _request(tmp_path, "fs_read", {"path": "alias.py"})
    )
    assert decision.decision_class is DecisionClass.A


def test_no_workspace_is_not_a(tmp_path: Path) -> None:
    tool = create_registry().get("fs_read")
    request = build_decision_request(tool, {"path": str(tmp_path / "a.py")})
    decision = DecisionClassifier(Boundary(workspace_root=None)).classify(request)
    assert decision.decision_class is DecisionClass.C


def test_borrowed_names_are_not_covered(tmp_path: Path) -> None:
    """A plugin or MCP tool named fs_read gets no free pass."""
    _tree(tmp_path)
    request = _request(tmp_path, "fs_read", {"path": "src/a.py"})
    classifier = DecisionClassifier(Boundary(workspace_root=tmp_path))
    assert classifier.classify(replace(request, provenance="plugin:evil")).decision_class is None
    mcp = classifier.classify(replace(request, provenance="mcp:srv"))
    assert mcp.rule is None or mcp.rule.id != "builtin-read"


def test_a_redeclared_read_tool_loses_the_grant(tmp_path: Path) -> None:
    _tree(tmp_path)
    request = _request(tmp_path, "fs_read", {"path": "src/a.py"})
    classifier = DecisionClassifier(Boundary(workspace_root=tmp_path))
    asked = classifier.classify(replace(request, side_effect_class="ask"))
    assert asked.decision_class is DecisionClass.B
    assert classifier.classify(replace(request, side_effect_class="never")).decision_class is (
        DecisionClass.C
    )


def test_mutation_or_host_is_not_a_read(tmp_path: Path) -> None:
    _tree(tmp_path)
    request = _request(tmp_path, "fs_read", {"path": "src/a.py"})
    classifier = DecisionClassifier(Boundary(workspace_root=tmp_path))
    hosted = classifier.classify(replace(request, hosts=frozenset({"example.com"})))
    assert hosted.decision_class is DecisionClass.C  # network-new-host
    mutating = classifier.classify(replace(request, is_mutation=True))
    assert mutating.rule is None or mutating.rule.id != "builtin-read"


def test_each_clause_of_the_rule_is_load_bearing(tmp_path: Path) -> None:
    """Cases the C rules don't catch first, so only builtin-read's own
    clauses keep them from A."""
    _tree(tmp_path)
    request = _request(tmp_path, "fs_read", {"path": "src/a.py"})
    allowed = DecisionClassifier(
        Boundary(workspace_root=tmp_path, allowed_hosts=frozenset({"example.com"}))
    )
    assert allowed.classify(replace(request, hosts=frozenset({"example.com"}))).rule is None
    stray_write = replace(request, writes=(tmp_path / "src" / "b.py",))
    assert allowed.classify(stray_write).rule is None
    assert allowed.classify(replace(request, reads=())).rule is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_inside_the_wall_is_a_from_the_raw_path(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "alias.py").symlink_to(tmp_path / "src" / "a.py")
    tool = create_registry().get("fs_read")
    request = build_decision_request(tool, {"path": str(tmp_path / "alias.py")})
    decision = DecisionClassifier(Boundary(workspace_root=tmp_path)).classify(request)
    assert decision.decision_class is DecisionClass.A


def test_cap_exceeded_still_stops_reads(tmp_path: Path) -> None:
    _tree(tmp_path)
    request = _request(tmp_path, "fs_read", {"path": "src/a.py"})
    decision = DecisionClassifier(Boundary(workspace_root=tmp_path, cap_exceeded=True)).classify(
        request
    )
    assert decision.decision_class is DecisionClass.C


def test_every_c_rule_precedes_the_read_grant() -> None:
    positions = {rule.id: i for i, rule in enumerate(RULE_TABLE)}
    for rule in RULE_TABLE:
        if rule.decision_class is DecisionClass.C:
            assert positions[rule.id] < positions["builtin-read"], rule.id


# ── No worker call ──────────────────────────────────────────────────────


async def test_worker_is_never_asked(tmp_path: Path) -> None:
    _tree(tmp_path)
    h = _Harness(tmp_path)
    for name, args in READS.items():
        decision = await h.dispatcher.classifier.classify(_request(tmp_path, name, args))
        assert decision.decision_class is DecisionClass.A
    assert h.worker.calls == []
    assert h.audits == []  # no judgment was made, so none is recorded


# ── Dispatch and policy ─────────────────────────────────────────────────


async def test_reads_dispatch_without_asking(tmp_path: Path) -> None:
    _tree(tmp_path)
    h = _Harness(tmp_path)
    for i, (name, args) in enumerate(READS.items()):
        result = await h.dispatcher.dispatch(f"c{i}", name, dict(args))
        assert result.status == "success", result.output
        assert result.decision_class is DecisionClass.A
    assert h.asked == []
    assert h.worker.calls == []


async def test_env_read_runs_by_default(tmp_path: Path) -> None:
    _tree(tmp_path)
    h = _Harness(tmp_path)
    result = await h.dispatcher.dispatch("c1", "fs_read", {"path": ".env"})
    assert result.status == "success"
    assert "TOKEN=needle" in result.output
    assert h.asked == []


async def test_ask_rule_for_env_still_asks(tmp_path: Path) -> None:
    _tree(tmp_path)
    policy = PolicyConfig(rules=[PolicyRule(tool="fs_read", args="*.env*", effect="ask")])
    h = _Harness(tmp_path, policy)
    env = await h.dispatcher.dispatch("c1", "fs_read", {"path": ".env"})
    src = await h.dispatcher.dispatch("c2", "fs_read", {"path": "src/a.py"})
    assert env.status == "success" and src.status == "success"
    assert h.asked == [("fs_read", "policy rule `fs_read: *.env*` → ask")]
    assert h.worker.calls == []


async def test_never_rule_for_env_refuses(tmp_path: Path) -> None:
    _tree(tmp_path)
    policy = PolicyConfig(rules=[PolicyRule(tool="fs_read", args="*.env*", effect="never")])
    h = _Harness(tmp_path, policy)
    result = await h.dispatcher.dispatch("c1", "fs_read", {"path": ".env"})
    assert result.status == "error"
    assert result.error_code == "policy_denied"
    assert "TOKEN" not in result.output
    assert h.asked == []


def test_policy_rule_beats_the_class_a_default(tmp_path: Path) -> None:
    tool = create_registry().get("fs_grep")
    for effect in ("ask", "never"):
        config = PolicyConfig(rules=[PolicyRule(tool="fs_grep", args="secrets/*", effect=effect)])
        decision = resolve_explained(
            config,
            tool,
            {"pattern": "x", "path": str(tmp_path / "secrets" / "a")},
            DecisionClass.A,
            tmp_path,
        )
        assert decision.effect == effect


async def test_outside_the_wall_is_still_refused(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.txt").write_text("x\n")
    h = _Harness(ws)
    result = await h.dispatcher.dispatch("c1", "fs_read", {"path": "../secret.txt"})
    assert result.status == "error"
    assert result.error_code == "boundary_refusal"
    assert result.decision_class is DecisionClass.C
    assert h.worker.calls == []


def test_ask_and_never_rules_ignore_case(tmp_path: Path) -> None:
    """On macOS and Windows `.ENV` opens `.env`; a guard must not miss it."""
    tool = create_registry().get("fs_read")
    for effect in ("ask", "never"):
        config = PolicyConfig(rules=[PolicyRule(tool="fs_read", args="*.env*", effect=effect)])
        for name in (".ENV", "config/.Env.local"):
            decision = resolve_explained(
                config, tool, {"path": str(tmp_path / name)}, DecisionClass.A, tmp_path
            )
            assert decision.effect == effect, (effect, name)


def test_auto_rules_stay_exact_about_case(tmp_path: Path) -> None:
    """Case never widens what runs without asking."""
    tool = create_registry().get("shell")
    config = PolicyConfig(rules=[PolicyRule(tool="shell", args="git status*", effect="auto")])
    decision = resolve_explained(config, tool, {"command": "GIT STATUS"}, DecisionClass.B)
    assert decision.effect == "ask"


async def test_grep_rule_guards_a_single_file_search(tmp_path: Path) -> None:
    """fs_grep on one file reads it whatever .gitignore says, so the
    documented .env guard names fs_grep as well as fs_read."""
    _tree(tmp_path)
    policy = PolicyConfig(
        rules=[
            PolicyRule(tool="fs_read", args="*.env*", effect="never"),
            PolicyRule(tool="fs_grep", args="*.env*", effect="never"),
        ]
    )
    h = _Harness(tmp_path, policy)
    result = await h.dispatcher.dispatch("c1", "fs_grep", {"pattern": "TOKEN", "path": ".env"})
    assert result.error_code == "policy_denied"
    assert "TOKEN=" not in result.output


async def test_never_still_refuses_in_autonomy(tmp_path: Path) -> None:
    """Autonomy runs ask as auto (TD-4101); never is what holds there."""
    _tree(tmp_path)
    policy = PolicyConfig(rules=[PolicyRule(tool="fs_read", args="*.env*", effect="never")])
    h = _Harness(tmp_path, policy)
    h.dispatcher.autonomy_fn = lambda: True
    result = await h.dispatcher.dispatch("c1", "fs_read", {"path": ".env"})
    assert result.error_code == "policy_denied"
