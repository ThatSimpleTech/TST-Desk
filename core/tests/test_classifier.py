"""Unit tests for the decision classifier rule table (TD-701).

Every rule is tested case by case per AGENTS.md §7 (the classifier is one
of the two areas that carry disproportionate correctness risk).  Each test
asserts both the resulting decision class *and* which rule fired, so a
mis-classification is diagnosable rather than a bare pass/fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.autonomy import (
    Boundary,
    Classification,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
)

WS = Path("/work/proj")


def classify(boundary: Boundary, request: DecisionRequest) -> Classification:
    return DecisionClassifier(boundary).classify(request)


def req(**kw: object) -> DecisionRequest:
    """Build a DecisionRequest from keyword defaults."""
    fields: dict[str, object] = {
        "tool_name": "tool",
        "arguments": {},
        "reads": (),
        "writes": (),
        "hosts": frozenset(),
        "is_mutation": False,
    }
    fields.update({k: v for k, v in kw.items() if v is not None})
    return DecisionRequest(**fields)  # type: ignore[arg-type]


def fired_as(decision: Classification, rule_id: str) -> bool:
    """Whether the decision was made by the rule with *rule_id*."""
    return decision.rule is not None and decision.rule.id == rule_id


def boundary(**kw: object) -> Boundary:
    fields: dict[str, object] = {"workspace_root": WS}
    fields.update(kw)
    return Boundary(**fields)  # type: ignore[arg-type]


# ── Class model ────────────────────────────────────────────────────────


def test_classes_are_the_three_spec_classes() -> None:
    assert {c.value for c in DecisionClass} == {"A", "B", "C"}


def test_class_values_are_the_string_labels() -> None:
    assert DecisionClass.A.value == "A"
    assert DecisionClass.B.value == "B"
    assert DecisionClass.C.value == "C"


# ── Rule: path outside workspace → C ───────────────────────────────────


class TestPathOutsideWorkspace:
    def test_write_outside_workspace_is_c(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_write", writes=(Path("/tmp/evil.txt"),), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "path-outside-workspace")

    def test_read_outside_workspace_is_c_even_read_only(self) -> None:
        # "any path outside workspace → C" applies to reads too.
        decision = classify(boundary(), req(tool_name="fs_read", reads=(Path("/etc/hosts"),)))
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "path-outside-workspace")

    def test_write_inside_workspace_is_not_this_rule(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_read", reads=(WS / "README.md",)),
        )
        assert not fired_as(decision, "path-outside-workspace")

    def test_parent_traversal_escaping_workspace_is_c(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_write", writes=(WS / "../outside.txt",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "path-outside-workspace")

    def test_symlink_inside_pointing_outside_is_c(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A symlink living inside the workspace that points outside resolves
        # outside and must be caught (mirrors TD-602 symlink traversal).
        #
        # The request path is workspace-relative and classified from inside
        # tmp_path: an absolute tmp_path is a drive-letter path on Windows,
        # where boundary-unsafe-path fires first (TD-1406) and masks the
        # rule under test.  Relative input pins path-outside-workspace on
        # every platform.
        monkeypatch.chdir(tmp_path)
        real_ws = tmp_path / "ws"
        real_ws.mkdir(exist_ok=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        (real_ws / "innocent_link.txt").symlink_to(outside)

        decision = classify(
            boundary(workspace_root=real_ws),
            req(tool_name="fs_write", writes=(Path("ws/innocent_link.txt"),), is_mutation=True),
        )
        # The request path lives inside the workspace lexically, but resolves
        # outside; the classifier must resolve symlinks and catch it.
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "path-outside-workspace")

    def test_no_workspace_means_every_path_outside(self) -> None:
        decision = classify(
            boundary(workspace_root=None),
            req(tool_name="fs_read", reads=(WS / "README.md",)),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "path-outside-workspace")


# ── Rule: network call to a new host → C ───────────────────────────────


class TestNetworkNewHost:
    def test_unallowlisted_host_is_c(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="http_get", hosts=frozenset({"example.com"})),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "network-new-host")

    def test_empty_allowlist_denies_all_network(self) -> None:
        # Empty allowed_hosts means no network calls are permitted.
        decision = classify(boundary(), req(tool_name="http_get", hosts=frozenset({"x.io"})))
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "network-new-host")

    def test_allowed_host_is_not_c(self) -> None:
        decision = classify(
            boundary(allowed_hosts=frozenset({"api.example.com"})),
            req(tool_name="http_get", hosts=frozenset({"api.example.com"})),
        )
        assert not fired_as(decision, "network-new-host")

    def test_no_network_intent_is_not_this_rule(self) -> None:
        decision = classify(boundary(), req(tool_name="fs_read", reads=(WS / "a.txt",)))
        assert not fired_as(decision, "network-new-host")


# ── Rule: steering-file write → C (even inside workspace) ──────────────


class TestSteeringFileWrite:
    def test_workspace_agents_md_is_c(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_edit", writes=(WS / "AGENTS.md",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "steering-file-write")

    def test_claude_md_is_c(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_write", writes=(WS / "CLAUDE.md",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "steering-file-write")

    def test_nested_agents_md_is_c(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_write", writes=(WS / "src" / "AGENTS.md",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "steering-file-write")

    def test_rules_dir_write_is_c(self) -> None:
        decision = classify(
            boundary(),
            req(
                tool_name="fs_write",
                writes=(WS / ".tst" / "rules" / "new.md",),
                is_mutation=True,
            ),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "steering-file-write")

    def test_reading_agents_md_is_not_c(self) -> None:
        # Reading a steering file is fine; only a *write* is refused.
        decision = classify(boundary(), req(tool_name="fs_read", reads=(WS / "AGENTS.md",)))
        assert not fired_as(decision, "steering-file-write")

    def test_plain_source_write_is_not_this_rule(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_edit", writes=(WS / "src" / "a.py",), is_mutation=True),
        )
        assert not fired_as(decision, "steering-file-write")

    def test_memory_write_is_not_this_rule(self) -> None:
        decision = classify(
            boundary(),
            req(
                tool_name="fs_write",
                writes=(WS / ".tst" / "memory" / "MEMORY.md",),
                is_mutation=True,
            ),
        )
        assert not fired_as(decision, "steering-file-write")
        assert decision.decision_class is DecisionClass.A
        assert fired_as(decision, "memory-file-write")


# ── Rule: cap exceeded → C ─────────────────────────────────────────────


class TestCapExceeded:
    def test_cap_exceeded_is_c_for_readable_action(self) -> None:
        # Cap C outranks even a reversible in-workspace action.
        decision = classify(
            boundary(cap_exceeded=True),
            req(tool_name="fs_edit", writes=(WS / "src" / "a.py",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "cap-exceeded")

    def test_caps_in_budget_not_this_rule(self) -> None:
        decision = classify(
            boundary(cap_exceeded=False),
            req(tool_name="fs_read", reads=(WS / "a.py",)),
        )
        assert not fired_as(decision, "cap-exceeded")


# ── Rule: in-workspace edit within writable_paths → A ──────────────────


class TestInWorkspaceEdit:
    def test_edit_inside_workspace_is_a(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_edit", writes=(WS / "src" / "a.py",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.A
        assert fired_as(decision, "in-workspace-edit")

    def test_edit_respecting_restrictive_writable_patterns_is_a(self) -> None:
        decision = classify(
            boundary(writable_patterns=("src/**",)),
            req(tool_name="fs_edit", writes=(WS / "src" / "a.py",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.A
        assert fired_as(decision, "in-workspace-edit")

    def test_write_outside_writable_patterns_is_not_a(self) -> None:
        # In-workspace but outside the declared writable path → not Class A.
        decision = classify(
            boundary(writable_patterns=("src/**",)),
            req(tool_name="fs_write", writes=(WS / "other" / "x.md",), is_mutation=True),
        )
        assert not fired_as(decision, "in-workspace-edit")

    def test_read_only_is_not_a(self) -> None:
        # An action that does not mutate is not a Class A edit.
        decision = classify(boundary(), req(tool_name="fs_read", reads=(WS / "a.py",)))
        assert not fired_as(decision, "in-workspace-edit")

    def test_mutation_with_no_write_targets_is_not_a(self) -> None:
        decision = classify(boundary(), req(tool_name="shell", is_mutation=True))
        assert not fired_as(decision, "in-workspace-edit")


# ── Rule: in-workspace write outside writable_paths → C (TD-602) ───────


class TestUnsafePath:
    def test_windows_unsafe_write_is_c(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_write", writes=(Path("C:foo"),), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "boundary-unsafe-path")

    def test_windows_unsafe_read_is_c(self) -> None:
        decision = classify(boundary(), req(tool_name="fs_read", reads=(Path(r"\\s\share\x"),)))
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "boundary-unsafe-path")

    def test_ordinary_path_is_not_this_rule(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_edit", writes=(WS / "src" / "a.py",), is_mutation=True),
        )
        assert not fired_as(decision, "boundary-unsafe-path")


class TestPathOutsideWritable:
    def test_write_outside_writable_patterns_is_c(self) -> None:
        # The charter's boundary forbids it → C, matching the guard refusal.
        decision = classify(
            boundary(writable_patterns=("src/**",)),
            req(tool_name="fs_write", writes=(WS / "other" / "x.md",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "path-outside-writable")

    def test_write_within_writable_patterns_is_not_c(self) -> None:
        decision = classify(
            boundary(writable_patterns=("src/**",)),
            req(tool_name="fs_write", writes=(WS / "src" / "a.py",), is_mutation=True),
        )
        assert not fired_as(decision, "path-outside-writable")

    def test_read_only_is_not_this_rule(self) -> None:
        decision = classify(
            boundary(writable_patterns=("src/**",)),
            req(tool_name="fs_read", reads=(WS / "lib" / "a.py",)),
        )
        assert not fired_as(decision, "path-outside-writable")

    def test_outside_workspace_outranks_writable(self) -> None:
        # A write outside the workspace is C via path-outside-workspace
        # (declared first), not via the writable rule.
        decision = classify(
            boundary(writable_patterns=("src/**",)),
            req(tool_name="fs_write", writes=(Path("/tmp/x"),), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "path-outside-workspace")

    def test_steering_write_outranks_writable(self) -> None:
        decision = classify(
            boundary(writable_patterns=("**",)),
            req(tool_name="fs_write", writes=(WS / "AGENTS.md",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "steering-file-write")


# ── Desktop computer-use (TD-3301) ─────────────────────────────────────


class TestDesktopComputerUse:
    def test_capture_is_class_a(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="desktop_screenshot", actuates=False),
        )
        assert decision.decision_class is DecisionClass.A
        assert fired_as(decision, "desktop-capture")

    def test_actuation_is_class_b(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="desktop_click", is_mutation=True, actuates=True),
        )
        assert decision.decision_class is DecisionClass.B
        assert fired_as(decision, "desktop-actuation")

    def test_cap_still_outranks_capture(self) -> None:
        decision = classify(
            boundary(cap_exceeded=True),
            req(tool_name="desktop_screenshot", actuates=False),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "cap-exceeded")


# ── Ambiguous cases → unclassified ─────────────────────────────────────


class TestSkillLoad:
    def test_load_skill_is_a_static_a(self) -> None:
        # Read-only by construction: name-keyed, human-written markdown,
        # no path fields. Without this rule every load burned a worker
        # classification round-trip and landed on the ask path.
        decision = classify(boundary(), req(tool_name="load_skill"))
        assert decision.decision_class is DecisionClass.A
        assert fired_as(decision, "skill-load")

    def test_skill_load_beats_in_workspace_edit(self) -> None:
        # Order pin: the load fires before the generic in-workspace rule
        # could claim a mutation-less request shape.
        decision = classify(
            boundary(),
            req(tool_name="load_skill", is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.A
        assert fired_as(decision, "skill-load")


class TestAmbiguous:
    def test_unless_an_unambiguous_rule_fires_goes_unclassified(self) -> None:
        # A mutation with no recognized shape: no rule fires, must be
        # decided by the worker-tier classifier (TD-703), defaulting to B.
        # (Not "shell": shell has a static B floor, TD-4805.)
        decision = classify(boundary(), req(tool_name="custom_tool", is_mutation=True))
        assert decision.decision_class is None
        assert decision.rule is None

    def test_unclassified_reason_is_recorded(self) -> None:
        decision = classify(boundary(), req(tool_name="generated", is_mutation=True))
        assert decision.reason == "no rule fired"


# ── Explainability ─────────────────────────────────────────────────────


class TestExplainability:
    def test_every_classification_names_the_rule_that_fired(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_write", writes=(Path("/tmp/x"),), is_mutation=True),
        )
        assert decision.rule is not None
        assert fired_as(decision, "path-outside-workspace")
        assert "[path-outside-workspace]" in decision.reason

    def test_class_a_decision_is_explainable(self) -> None:
        decision = classify(
            boundary(),
            req(tool_name="fs_edit", writes=(WS / "src" / "a.py",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.A
        assert decision.reason.startswith("[in-workspace-edit]")


# ── Priority: irreversible wins over reversible ────────────────────────


class TestPriority:
    def test_steering_write_beats_in_workspace_edit(self) -> None:
        # AGENTS.md is inside the workspace and matches default writable
        # patterns, but the steering C rule must win over the A rule.
        decision = classify(
            boundary(),
            req(tool_name="fs_write", writes=(WS / "AGENTS.md",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "steering-file-write")

    def test_cap_beats_memory_write(self) -> None:
        decision = classify(
            boundary(cap_exceeded=True),
            req(
                tool_name="fs_write",
                writes=(WS / ".tst" / "memory" / "gotchas.md",),
                is_mutation=True,
            ),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "cap-exceeded")

    def test_cap_beats_in_workspace_edit(self) -> None:
        decision = classify(
            boundary(cap_exceeded=True),
            req(tool_name="fs_edit", writes=(WS / "src" / "a.py",), is_mutation=True),
        )
        assert decision.decision_class is DecisionClass.C
        assert fired_as(decision, "cap-exceeded")


# ── writable glob semantics ────────────────────────────────────────────


class TestWritableGlobs:
    @pytest.mark.parametrize(
        ("patterns", "target", "expected"),
        [
            (("**",), "src/a.py", True),
            (("src/**",), "src/a.py", True),
            (("src/**",), "src/deep/a.py", True),
            (("src/**",), "lib/a.py", False),
            (("*.py",), "a.py", True),
            (("*.py",), "deep/a.py", True),  # slash-less matches basename at depth
            (("src/*.py",), "src/a.py", True),
            (("src/*.py",), "src/deep/a.py", False),
        ],
    )
    def test_glob_matching(self, patterns: tuple[str, ...], target: str, expected: bool) -> None:
        decision = classify(
            boundary(writable_patterns=patterns),
            req(tool_name="fs_write", writes=(WS / target,), is_mutation=True),
        )
        if expected:
            assert decision.decision_class is DecisionClass.A
            assert fired_as(decision, "in-workspace-edit")
        else:
            assert not fired_as(decision, "in-workspace-edit")
