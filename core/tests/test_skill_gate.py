"""Which Claude Code skills reach the brain, and what a loaded one carries (TD-5011).

``~/.claude/skills`` is Claude Code's user-global tree, so it is read only
with ``steering.claude_global_fallback`` (TD-4845's key, default false).
A workspace's own ``.claude/skills`` stays on, and on the home folder that
tree is the workspace's own.  A loaded skill names its directory, so a body
that runs ``python3 chapters.py`` can find the file.  A skill over the body
cap stays out of the catalog, since ``load_skill`` would refuse it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tstd.config import ModelConfig, SteeringConfig
from tstd.context.prompt import PromptAssembler
from tstd.context.skills import (
    SKILL_BODY_TOKEN_CAP,
    SkillDiscoverer,
    apply_slash_skill,
    render_skill_catalog,
    try_load_skill,
)
from tstd.session import Session


def _skill(root: Path, name: str, body: str = "Run `python3 chapters.py`.\n") -> Path:
    path = root / ".claude" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Do {name}.\n---\n{body}", encoding="utf-8")
    return path


def _session(ws: Path, *, claude_global_fallback: bool) -> Session:
    session = Session(str(ws))
    steering = SteeringConfig(claude_global_fallback=claude_global_fallback)
    # Only `steering` is read on this path; the presets are not.
    session.config = ModelConfig.model_construct(presets={}, steering=steering)
    return session


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws, tmp_path / "home"


class TestUserGlobalGate:
    def test_user_global_claude_tree_is_off_by_default(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _skill(home, "yt-edit")
        assert SkillDiscoverer(home_dir=home).discover(ws) == []
        brain = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert "yt-edit" not in brain.text

    def test_the_steering_key_turns_it_on(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _skill(home, "yt-edit")
        found = SkillDiscoverer(home_dir=home, claude_global_fallback=True).discover(ws)
        assert [(s.name, s.source) for s in found] == [("yt-edit", "claude_user")]
        brain = PromptAssembler(ws, home_dir=home, claude_global_fallback=True).assemble_sync(
            "brain"
        )
        assert "`yt-edit`" in brain.text

    def test_workspace_claude_tree_stays_on(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _skill(ws, "review")
        found = SkillDiscoverer(home_dir=home).discover(ws)
        assert [(s.name, s.source) for s in found] == [("review", "claude_workspace")]

    def test_home_folder_workspace_loads_its_own_claude_tree(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _skill(home, "yt-edit")
        found = SkillDiscoverer(home_dir=home).discover(home)
        assert [(s.name, s.source) for s in found] == [("yt-edit", "claude_workspace")]
        session = _session(home, claude_global_fallback=False)
        assert try_load_skill(session, "yt-edit", home_dir=home).startswith("Loaded skill")

    def test_load_and_slash_follow_the_session_config(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _skill(home, "yt-edit")
        off = _session(ws, claude_global_fallback=False)
        assert try_load_skill(off, "yt-edit", home_dir=home).startswith("Error: unknown skill")
        assert apply_slash_skill(off, "/yt-edit now", home_dir=home) is None
        on = _session(ws, claude_global_fallback=True)
        assert try_load_skill(on, "yt-edit", home_dir=home).startswith("Loaded skill")
        on_slash = _session(ws, claude_global_fallback=True)
        result = apply_slash_skill(on_slash, "/yt-edit now", home_dir=home)
        assert result is not None and result.startswith("Loaded skill")


class TestSkillDirectory:
    def test_user_skill_names_its_directory_in_tilde_form(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _skill(home, "yt-chapters")
        assembler = PromptAssembler(ws, home_dir=home, claude_global_fallback=True)
        brain = assembler.assemble_sync("brain", loaded_skills=["yt-chapters"])
        assert "Skill directory: ~/.claude/skills/yt-chapters " in brain.text
        assert "relative paths in this skill are relative to it" in brain.text
        assert str(home) not in brain.text

    def test_workspace_skill_names_a_workspace_relative_directory(
        self, dirs: tuple[Path, Path]
    ) -> None:
        ws, home = dirs
        _skill(ws, "review")
        brain = PromptAssembler(ws, home_dir=home).assemble_sync("brain", loaded_skills=["review"])
        assert "Skill directory: .claude/skills/review " in brain.text

    def test_catalog_alone_carries_no_directory(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _skill(ws, "review")
        brain = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert "`review`" in brain.text
        assert "Skill directory" not in brain.text


class TestOverCap:
    def test_over_cap_skill_is_left_out_of_the_catalog_and_logged_once(
        self, dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, home = dirs
        _skill(ws, "small")
        path = _skill(ws, "huge", body="x" * (SKILL_BODY_TOKEN_CAP * 4 + 8))
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        with caplog.at_level(logging.WARNING, logger="tstd.skills"):
            first = render_skill_catalog(skills)
            second = render_skill_catalog(skills)
        assert first == second
        assert "`small`" in first and "`huge`" not in first
        notes = [r for r in caplog.records if "over the body cap" in r.getMessage()]
        assert len(notes) == 1
        assert notes[0].__dict__["extra_fields"]["path"] == str(path)

    def test_only_over_cap_skills_means_no_catalog(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _skill(ws, "huge", body="x" * (SKILL_BODY_TOKEN_CAP * 4 + 8))
        assert render_skill_catalog(SkillDiscoverer(home_dir=home).discover(ws)) == ""

    def test_a_typed_slash_still_explains_the_refusal(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _skill(ws, "huge", body="x" * (SKILL_BODY_TOKEN_CAP * 4 + 8))
        result = apply_slash_skill(Session(str(ws)), "/huge", home_dir=home)
        assert result is not None and "over budget" in result


class TestSkipWarnings:
    def test_an_ignored_key_is_logged_once_not_every_prompt(
        self, dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        # Since TD-5107 the skill loads; the warning about its key is still once.
        ws, home = dirs
        path = ws / ".claude" / "skills" / "odd" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\ndescription: Odd\nmodel: haiku\n---\nbody\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="tstd.skills"):
            for _ in range(3):
                prompt = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert "- `odd`: Odd" in prompt.text
        warnings = [r for r in caplog.records if "keys ignored" in r.getMessage()]
        assert len(warnings) == 1
