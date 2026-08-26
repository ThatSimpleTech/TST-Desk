"""Steering-guard hardening (TD-4803, TD-4804).

TD-4803: ``.tst/config.yaml`` holds the approval policy, so a write to it
rewrites the guardrails — the self-escalation the steering refusal exists
to prevent. It is refused like a steering file; the daemon's own scaffold
path writes directly and is unaffected.

TD-4804: the directory comparisons case-fold. On APFS/NTFS a verbatim
tuple match lets ``.tst/RULES/…`` past the check while the filesystem
lands it in ``.tst/rules/``. Over-refusal of a literal case-variant
directory on a case-sensitive filesystem is accepted — fail closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_security_suite import make_dispatcher
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier, DecisionRequest
from tstd.autonomy.classifier import is_memory_write, is_steering_write
from tstd.boundary_config import scaffold_workspace_config
from tstd.tools.boundary import PathGuard, RefusalError


def _boundary(ws: Path, writable: tuple[str, ...] = ("**",)) -> Boundary:
    return Boundary(workspace_root=ws, writable_patterns=writable)


class TestCaseFoldedDirectories:
    @pytest.mark.parametrize("dirname", ["RULES", "Rules", "rUlEs"])
    def test_rules_dir_case_variants_are_steering(self, tmp_path: Path, dirname: str) -> None:
        b = _boundary(tmp_path)
        target = tmp_path / ".tst" / dirname / "api.md"
        assert is_steering_write(b, target)
        assert not is_memory_write(b, target)

    @pytest.mark.parametrize("dirname", ["MEMORY", "Memory"])
    def test_memory_dir_case_variants_stay_memory(self, tmp_path: Path, dirname: str) -> None:
        b = _boundary(tmp_path)
        target = tmp_path / ".tst" / dirname / "gotchas.md"
        assert is_memory_write(b, target)
        assert not is_steering_write(b, target)

    def test_steering_basename_wins_under_case_variant_memory(self, tmp_path: Path) -> None:
        b = _boundary(tmp_path)
        planted = tmp_path / ".tst" / "MEMORY" / "agents.md"
        assert is_steering_write(b, planted)
        assert not is_memory_write(b, planted)

    def test_classifier_refuses_case_variant_rules_write(self, tmp_path: Path) -> None:
        decision = DecisionClassifier(_boundary(tmp_path)).classify(
            DecisionRequest(
                tool_name="fs_write",
                writes=(tmp_path / ".tst" / "RULES" / "evil.md",),
                is_mutation=True,
            )
        )
        assert decision.decision_class is DecisionClass.C
        assert decision.rule is not None
        assert decision.rule.id == "steering-file-write"

    def test_guard_refuses_case_variant_rules_write(self, tmp_path: Path) -> None:
        g = PathGuard(_boundary(tmp_path))
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / ".tst" / "RULES" / "evil.md")
        assert ei.value.code == "steering_file"


class TestPolicyFileGuard:
    def test_config_yaml_is_steering(self, tmp_path: Path) -> None:
        b = _boundary(tmp_path)
        assert is_steering_write(b, tmp_path / ".tst" / "config.yaml")

    def test_config_yaml_case_variant_is_steering(self, tmp_path: Path) -> None:
        b = _boundary(tmp_path)
        assert is_steering_write(b, tmp_path / ".tst" / "CONFIG.YAML")

    def test_sibling_yaml_is_not_steering(self, tmp_path: Path) -> None:
        b = _boundary(tmp_path)
        assert not is_steering_write(b, tmp_path / ".tst" / "other.yaml")

    def test_classifier_marks_policy_write_c(self, tmp_path: Path) -> None:
        decision = DecisionClassifier(_boundary(tmp_path)).classify(
            DecisionRequest(
                tool_name="fs_write",
                writes=(tmp_path / ".tst" / "config.yaml",),
                is_mutation=True,
            )
        )
        assert decision.decision_class is DecisionClass.C
        assert decision.rule is not None
        assert decision.rule.id == "steering-file-write"

    def test_guard_refuses_policy_write(self, tmp_path: Path) -> None:
        g = PathGuard(_boundary(tmp_path))
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / ".tst" / "config.yaml")
        assert ei.value.code == "steering_file"

    async def test_policy_poisoning_write_refused_end_to_end(self, tmp_path: Path) -> None:
        """A write promoting Class C to auto never leaves the dispatcher."""
        dispatcher = make_dispatcher(tmp_path)
        config = tmp_path / ".tst" / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("policy:\n  class_c_default: ask\n", encoding="utf-8")

        refused = await dispatcher.dispatch(
            "p1",
            "fs_write",
            {"path": str(config), "content": "policy:\n  class_c_default: auto\n"},
        )
        assert refused.status == "error"
        assert refused.error_code == "boundary_refusal"
        assert refused.decision_class is DecisionClass.C
        assert "ask" in config.read_text(encoding="utf-8")

    def test_daemon_scaffold_path_unaffected(self, tmp_path: Path) -> None:
        """The daemon plants the template directly, not through the guard."""
        written = scaffold_workspace_config(tmp_path)
        assert written is not None
        assert written.name == "config.yaml"
        assert written.exists()


class TestTrailingCharAliases:
    """Trailing dot/space components alias steering files on Windows
    (Win32 strips them at open time); the guard refuses them on every
    platform (TD-4820)."""

    @pytest.mark.parametrize(
        "raw",
        [
            ".tst/config.yaml.",
            "AGENTS.md.",
            ".tst./config.yaml",
            ".tst/rules./x.md",
            "notes.md.",  # not steering — still an unsafe form
            "dir /x.md",  # trailing space in a component
        ],
    )
    def test_trailing_char_forms_refused(self, tmp_path: Path, raw: str) -> None:
        g = PathGuard(_boundary(tmp_path))
        with pytest.raises(RefusalError) as ei:
            g.check_write(raw)
        assert ei.value.code == "windows_unsafe"

    @pytest.mark.parametrize(
        "raw",
        [
            "notes.md",
            "./src/x.py",  # leading ./ is navigation, not a component
            "dir/../x.md",
            ".tst/memory/MEMORY.md",
        ],
    )
    def test_ordinary_dotted_and_navigated_paths_unaffected(self, tmp_path: Path, raw: str) -> None:
        g = PathGuard(_boundary(tmp_path))
        g.check_write(raw)  # no refusal

    def test_bare_policy_file_still_refuses_as_steering_not_unsafe(self, tmp_path: Path) -> None:
        # Unaffected by the new rule: the pre-existing steering refusal
        # still fires, not a windows_unsafe one.
        g = PathGuard(_boundary(tmp_path))
        with pytest.raises(RefusalError) as ei:
            g.check_write(".tst/config.yaml")
        assert ei.value.code == "steering_file"

    def test_classifier_marks_trailing_dot_steering_alias_c(self, tmp_path: Path) -> None:
        decision = DecisionClassifier(_boundary(tmp_path)).classify(
            DecisionRequest(
                tool_name="fs_write",
                writes=(tmp_path / "AGENTS.md.",),
                is_mutation=True,
            )
        )
        assert decision.decision_class is DecisionClass.C
        assert decision.rule is not None
        assert decision.rule.id == "boundary-unsafe-path"


class TestCommandFileGuard:
    """Slash-command trees are Class C writes (TD-4501)."""

    @pytest.mark.parametrize(
        "relative",
        [
            Path(".tst") / "commands" / "review.md",
            Path(".claude") / "commands" / "review.md",
            Path(".tst") / "COMMANDS" / "review.md",
        ],
    )
    def test_command_paths_are_steering(self, tmp_path: Path, relative: Path) -> None:
        b = _boundary(tmp_path)
        target = tmp_path / relative
        assert is_steering_write(b, target)

    def test_classifier_refuses_command_write(self, tmp_path: Path) -> None:
        decision = DecisionClassifier(_boundary(tmp_path)).classify(
            DecisionRequest(
                tool_name="fs_write",
                writes=(tmp_path / ".tst" / "commands" / "evil.md",),
                is_mutation=True,
            )
        )
        assert decision.decision_class is DecisionClass.C
        assert decision.rule is not None
        assert decision.rule.id == "steering-file-write"

    def test_guard_refuses_command_write(self, tmp_path: Path) -> None:
        g = PathGuard(_boundary(tmp_path))
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / ".tst" / "commands" / "evil.md")
        assert ei.value.code == "steering_file"

    def test_fs_edit_target_is_also_refused(self, tmp_path: Path) -> None:
        g = PathGuard(_boundary(tmp_path))
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / ".claude" / "commands" / "review.md")
        assert ei.value.code == "steering_file"
