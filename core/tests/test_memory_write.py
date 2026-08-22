"""Memory write permission (TD-2102).

``.tst/memory/**`` is Class A and allowed through the guard. Steering
files stay Class C and refused — including in the same dispatcher
session after a successful memory write. The carve-out is the
classifier table plus the guard, not a handler special case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_security_suite import make_dispatcher
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier, DecisionRequest
from tstd.autonomy.classifier import is_memory_write, is_steering_write
from tstd.tools.boundary import PathGuard, RefusalError


def _boundary(ws: Path, writable: tuple[str, ...] = ("**",)) -> Boundary:
    return Boundary(workspace_root=ws, writable_patterns=writable)


class TestPredicates:
    def test_memory_paths_are_memory_not_steering(self, tmp_path: Path) -> None:
        b = _boundary(tmp_path)
        for rel in ("MEMORY.md", "decisions.md", "gotchas.md", "auth.md"):
            target = tmp_path / ".tst" / "memory" / rel
            assert is_memory_write(b, target)
            assert not is_steering_write(b, target)

    def test_steering_basenames_stay_steering_even_under_memory(self, tmp_path: Path) -> None:
        b = _boundary(tmp_path)
        planted = tmp_path / ".tst" / "memory" / "AGENTS.md"
        assert is_steering_write(b, planted)
        assert not is_memory_write(b, planted)

    def test_skill_manifest_is_steering_and_not_memory(self, tmp_path: Path) -> None:
        # SKILL.md joined the steering set in TD-4502; every copy of the
        # basenames — including this exclusion here — must agree, or a
        # .tst/memory/SKILL.md becomes indexable prompt material.
        b = _boundary(tmp_path)
        planted = tmp_path / ".tst" / "memory" / "SKILL.md"
        assert is_steering_write(b, planted)
        assert not is_memory_write(b, planted)

    def test_rules_dir_is_still_steering(self, tmp_path: Path) -> None:
        b = _boundary(tmp_path)
        rule = tmp_path / ".tst" / "rules" / "api.md"
        assert is_steering_write(b, rule)
        assert not is_memory_write(b, rule)


class TestClassifierTable:
    def test_memory_write_is_class_a_named_rule(self, tmp_path: Path) -> None:
        decision = DecisionClassifier(_boundary(tmp_path)).classify(
            DecisionRequest(
                tool_name="fs_write",
                writes=(tmp_path / ".tst" / "memory" / "MEMORY.md",),
                is_mutation=True,
            )
        )
        assert decision.decision_class is DecisionClass.A
        assert decision.rule is not None
        assert decision.rule.id == "memory-file-write"

    def test_memory_edit_is_class_a(self, tmp_path: Path) -> None:
        decision = DecisionClassifier(_boundary(tmp_path)).classify(
            DecisionRequest(
                tool_name="fs_edit",
                writes=(tmp_path / ".tst" / "memory" / "gotchas.md",),
                is_mutation=True,
            )
        )
        assert decision.decision_class is DecisionClass.A
        assert decision.rule is not None
        assert decision.rule.id == "memory-file-write"

    def test_tight_writable_paths_still_class_a(self, tmp_path: Path) -> None:
        decision = DecisionClassifier(_boundary(tmp_path, writable=("src/**",))).classify(
            DecisionRequest(
                tool_name="fs_write",
                writes=(tmp_path / ".tst" / "memory" / "decisions.md",),
                is_mutation=True,
            )
        )
        assert decision.decision_class is DecisionClass.A
        assert decision.rule is not None
        assert decision.rule.id == "memory-file-write"

    def test_rule_is_in_the_table_not_a_handler(self) -> None:
        from tstd.autonomy.classifier import RULE_TABLE

        ids = [rule.id for rule in RULE_TABLE]
        assert "memory-file-write" in ids
        assert ids.index("memory-file-write") > ids.index("steering-file-write")
        assert ids.index("memory-file-write") > ids.index("path-outside-writable")


class TestGuard:
    def test_memory_write_allowed(self, tmp_path: Path) -> None:
        g = PathGuard(_boundary(tmp_path))
        target = tmp_path / ".tst" / "memory" / "MEMORY.md"
        assert g.check_write(target) == g.canonicalize(target)

    def test_memory_write_allowed_when_writable_is_tight(self, tmp_path: Path) -> None:
        g = PathGuard(_boundary(tmp_path, writable=("src/**",)))
        target = tmp_path / ".tst" / "memory" / "gotchas.md"
        assert g.check_write(target) == g.canonicalize(target)

    def test_steering_still_refused(self, tmp_path: Path) -> None:
        g = PathGuard(_boundary(tmp_path))
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / "AGENTS.md")
        assert ei.value.code == "steering_file"


class TestSameSession:
    async def test_memory_write_then_steering_refused(self, tmp_path: Path) -> None:
        """One dispatcher: memory lands, then AGENTS.md / rules do not."""
        dispatcher = make_dispatcher(tmp_path)
        memory = tmp_path / ".tst" / "memory" / "gotchas.md"
        wrote = await dispatcher.dispatch(
            "m1",
            "fs_write",
            {"path": str(memory), "content": "the cache key includes the preset\n"},
        )
        assert wrote.status == "success"
        assert wrote.decision_class is DecisionClass.A
        assert memory.read_text(encoding="utf-8") == "the cache key includes the preset\n"

        edited = await dispatcher.dispatch(
            "m2",
            "fs_edit",
            {
                "path": str(memory),
                "old_string": "the cache key includes the preset",
                "new_string": "the cache key includes the preset and the root",
            },
        )
        assert edited.status == "success"
        assert edited.decision_class is DecisionClass.A

        agents = tmp_path / "AGENTS.md"
        agents.write_text("existing\n", encoding="utf-8")
        refused = await dispatcher.dispatch(
            "s1",
            "fs_write",
            {"path": str(agents), "content": "override\n"},
        )
        assert refused.status == "error"
        assert refused.error_code == "boundary_refusal"
        assert refused.decision_class is DecisionClass.C
        assert agents.read_text(encoding="utf-8") == "existing\n"

        rule = tmp_path / ".tst" / "rules" / "api.md"
        rules_refused = await dispatcher.dispatch(
            "s2",
            "fs_write",
            {"path": str(rule), "content": "never\n"},
        )
        assert rules_refused.status == "error"
        assert rules_refused.decision_class is DecisionClass.C
        assert not rule.exists()
        # Memory bytes survived the refused steering writes.
        assert "cache key" in memory.read_text(encoding="utf-8")
