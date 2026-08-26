"""Skill discovery, catalog, load_skill, slash, and Class C writes (TD-4502)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_read_tools import make_dispatcher
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier, DecisionRequest
from tstd.autonomy.classifier import is_steering_write
from tstd.context.prompt import PromptAssembler
from tstd.context.skills import (
    SKILL_BODY_TOKEN_CAP,
    SKILL_LOAD_MARKER,
    SkillDiscoverer,
    apply_slash_skill,
    list_workspace_skills,
    try_load_skill,
)
from tstd.context.stack import build_instruction_stack
from tstd.session import Session
from tstd.tools.boundary import PathGuard, RefusalError
from tstd.tools.registry import create_registry


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _skill_md(*, description: str, when: str, body: str) -> str:
    return f"---\ndescription: {description}\nwhenToUse: {when}\n---\n{body}"


def _discover(ws: Path, home: Path) -> list[str]:
    return [s.name for s in SkillDiscoverer(home_dir=home).discover(ws)]


class TestDiscovery:
    def test_workspace_skill_appears(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            _skill_md(description="Review a PR", when="After a diff", body="Check the tests.\n"),
        )
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [s.name for s in skills] == ["review"]
        assert skills[0].description == "Review a PR"
        assert skills[0].when_to_use == "After a diff"
        assert skills[0].body == "Check the tests.\n"
        assert skills[0].source == "workspace"

    def test_user_global_wins_on_name(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            _skill_md(description="workspace", when="ws", body="workspace body\n"),
        )
        _write(
            home / ".tstdesk" / "skills" / "review" / "SKILL.md",
            _skill_md(description="user", when="user", body="user body\n"),
        )
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert len(skills) == 1
        assert skills[0].name == "review"
        assert skills[0].body == "user body\n"
        assert skills[0].source == "user"
        assert skills[0].description == "user"

    def test_fallback_claude_only_when_ours_empty(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(
            ws / ".claude" / "skills" / "review" / "SKILL.md",
            _skill_md(description="claude", when="fallback", body="claude body\n"),
        )
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [s.name for s in skills] == ["review"]
        assert skills[0].source == "claude_workspace"
        assert skills[0].body == "claude body\n"

    def test_fallback_not_used_when_ours_has_skill_md(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(
            ws / ".tst" / "skills" / "ship" / "SKILL.md",
            _skill_md(description="ours", when="now", body="ours\n"),
        )
        _write(
            ws / ".claude" / "skills" / "review" / "SKILL.md",
            _skill_md(description="claude", when="never", body="claude\n"),
        )
        assert _discover(ws, home) == ["ship"]

    def test_extra_frontmatter_key_omits_skill(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            "---\ndescription: Review\nwhenToUse: After a diff\nlicense: MIT\n---\nbody\n",
        )
        _write(
            ws / ".tst" / "skills" / "ship" / "SKILL.md",
            _skill_md(description="Ship", when="Ready", body="Ship it.\n"),
        )
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [s.name for s in skills] == ["ship"]

    def test_missing_required_key_skips(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            "---\ndescription: Review\n---\nbody\n",
        )
        assert _discover(ws, home) == []


class TestPromptCatalog:
    def test_brain_catalog_without_bodies_until_load(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        body = "SECRET-SKILL-BODY-MUST-NOT-APPEAR\n"
        _write(ws / "AGENTS.md", "workspace steering\n")
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            _skill_md(description="Review a PR", when="After a diff", body=body),
        )
        prompt = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert "review" in prompt.text
        assert "Review a PR" in prompt.text
        assert body.strip() not in prompt.text
        assert "SECRET-SKILL-BODY-MUST-NOT-APPEAR" not in prompt.prefix

    def test_load_puts_body_after_prefix_hash_stable(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        body = "LOADED-SKILL-BODY\n"
        _write(ws / "AGENTS.md", "workspace steering\n")
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            _skill_md(description="Review a PR", when="After a diff", body=body),
        )
        assembler = PromptAssembler(ws, home_dir=home)
        before = assembler.assemble_sync("brain")
        after = assembler.assemble_sync("brain", loaded_skills=["review"])
        assert after.prefix_hash == before.prefix_hash
        assert after.prefix == before.prefix
        assert body.strip() in after.text
        assert after.text.index(after.prefix) == 0
        assert after.text.index(body.strip()) > len(after.prefix)
        assert "Review a PR" in after.text

    def test_worker_prompt_has_no_catalog(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / "AGENTS.md", "workspace steering\n")
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            _skill_md(description="Review a PR", when="After a diff", body="body\n"),
        )
        worker = PromptAssembler(ws, home_dir=home).assemble_sync("worker")
        assert "Review a PR" not in worker.text
        assert "## Skills" not in worker.text
        assert "load_skill" not in worker.text


class TestLoadSkill:
    def test_over_budget_refused_body_absent(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        huge = "x" * (SKILL_BODY_TOKEN_CAP * 4 + 8)
        _write(
            ws / ".tst" / "skills" / "huge" / "SKILL.md",
            _skill_md(description="Too big", when="Never", body=huge),
        )
        session = Session(str(ws))
        result = try_load_skill(session, "huge", home_dir=home)
        assert "over budget" in result
        assert "not truncated" in result
        assert session.loaded_skills == []
        prompt = PromptAssembler(ws, home_dir=home).assemble_sync(
            "brain", loaded_skills=session.loaded_skills
        )
        assert huge not in prompt.text

    def test_load_twice_is_noop(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            _skill_md(description="Review", when="Now", body="body\n"),
        )
        session = Session(str(ws))
        first = try_load_skill(session, "review", home_dir=home)
        second = try_load_skill(session, "review", home_dir=home)
        assert "Loaded skill" in first
        assert "already loaded" in second
        assert session.loaded_skills == ["review"]

    async def test_tool_over_budget_via_dispatch(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        huge = "y" * (SKILL_BODY_TOKEN_CAP * 4 + 8)
        _write(
            ws / ".tst" / "skills" / "huge" / "SKILL.md",
            _skill_md(description="Too big", when="Never", body=huge),
        )
        session = Session(str(ws))
        dispatcher = make_dispatcher(ws)
        result = await dispatcher.dispatch("c1", "load_skill", {"name": "huge"}, session)
        assert result.status == "success"
        assert "over budget" in result.output
        assert session.loaded_skills == []


class TestSlash:
    def test_slash_loads_skill_that_is_not_a_command(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            _skill_md(description="Review", when="Now", body="skill body\n"),
        )
        session = Session(str(ws))
        result = apply_slash_skill(session, "/review", home_dir=home)
        assert result is not None
        assert "Loaded skill" in result
        assert session.loaded_skills == ["review"]

    def test_command_wins_on_same_stem(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / ".tst" / "commands" / "review.md", "command body\n")
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            _skill_md(description="Review", when="Now", body="skill body\n"),
        )
        session = Session(str(ws))
        assert apply_slash_skill(session, "/review", home_dir=home) is None
        assert session.loaded_skills == []

    def test_load_marker_also_loads(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(
            ws / ".tst" / "skills" / "ship" / "SKILL.md",
            _skill_md(description="Ship", when="Ready", body="go\n"),
        )
        session = Session(str(ws))
        marker = SKILL_LOAD_MARKER.format(name="ship")
        assert apply_slash_skill(session, marker, home_dir=home) is not None
        assert session.loaded_skills == ["ship"]


class TestClassC:
    def test_tst_skill_md_write_is_class_c_and_guard_refuses(self, tmp_path: Path) -> None:
        target = tmp_path / ".tst" / "skills" / "foo" / "SKILL.md"
        b = Boundary(workspace_root=tmp_path, writable_patterns=("**",))
        assert is_steering_write(b, target)
        decision = DecisionClassifier(b).classify(
            DecisionRequest(tool_name="fs_write", writes=(target,), is_mutation=True)
        )
        assert decision.decision_class is DecisionClass.C
        assert decision.rule is not None
        assert decision.rule.id == "steering-file-write"
        with pytest.raises(RefusalError) as ei:
            PathGuard(b).check_write(target)
        assert ei.value.code == "steering_file"

    def test_nested_src_skill_md_is_class_c(self, tmp_path: Path) -> None:
        target = tmp_path / "src" / "SKILL.md"
        b = Boundary(workspace_root=tmp_path, writable_patterns=("**",))
        assert is_steering_write(b, target)
        with pytest.raises(RefusalError) as ei:
            PathGuard(b).check_write(target)
        assert ei.value.code == "steering_file"

    def test_ordinary_markdown_is_not_a_skill_write(self, tmp_path: Path) -> None:
        b = Boundary(workspace_root=tmp_path, writable_patterns=("**",))
        assert not is_steering_write(b, tmp_path / "notes.md")
        assert not is_steering_write(b, tmp_path / ".tst" / "memory" / "gotchas.md")


class TestInstructionStack:
    def test_skills_listed_separately_from_steering(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / "AGENTS.md", "workspace steering\n")
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            _skill_md(description="Review a PR", when="After a diff", body="body\n"),
        )
        assembled = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        skills = list_workspace_skills(ws, home)
        stack = build_instruction_stack(
            "sess-1",
            assembled.steering,
            skills=skills,
            loaded_skill_names=[],
        )
        assert all("SKILL.md" not in s.path for s in stack.sources)
        assert [s.name for s in stack.skills] == ["review"]
        assert stack.skills[0].description == "Review a PR"
        assert stack.skills[0].source == "workspace"
        assert stack.skills[0].loaded is False
        assert stack.skills[0].tokens > 0

        loaded = build_instruction_stack(
            "sess-1",
            assembled.steering,
            skills=skills,
            loaded_skill_names=["review"],
        )
        assert loaded.skills[0].loaded is True


class TestRegistry:
    def test_load_skill_is_builtin_auto(self) -> None:
        tool = create_registry().get("load_skill")
        assert tool is not None
        assert tool.side_effect_class == "auto"
        assert tool.mutates is False
