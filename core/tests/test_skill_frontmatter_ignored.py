"""Skills with frontmatter TST Desk does not act on load and warn (TD-5107).

The Agent Skills spec asks clients to load a skill with fields they do not
recognise and warn, not reject it.  TD-5011 still skipped a skill carrying
any key that would restrict or reroute it (``model``, ``hooks``,
``context``, ``disallowed-tools``) or any unknown key, so imported skills
vanished with a log line.  Now they load; the keys they carried that
nothing acts on are in ``Skill.ignored_keys``, the inspector's
``SkillStackEntry.ignored_keys`` and the doctor's ``skills`` row.
``when_to_use`` and ``compatibility`` reach the model beside the
description.  ``disable-model-invocation`` is honored, the strict way: the
skill loads but only the user's ``/name`` can attach it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tstd.context.prompt import PromptAssembler
from tstd.context.skills import (
    SkillDiscoverer,
    apply_slash_skill,
    handle_load_skill,
    list_workspace_skills,
    render_skill_catalog,
    skills_doctor_detail,
    try_load_skill,
)
from tstd.context.stack import build_instruction_stack
from tstd.session import Session

# A Claude Code skill using the keys its docs list, several of which TST
# Desk cannot honor: a model switch, a forked subagent, skill hooks.
CLAUDE_STYLE = """\
---
name: release-notes
description: Write release notes from the merged pull requests.
when_to_use: When the user asks for release notes or a changelog entry.
argument-hint: "[version]"
allowed-tools: Bash(git log:*) Read
model: claude-opus-4-1
effort: high
context: fork
agent: general-purpose
disallowed-tools: WebFetch
paths: ["CHANGELOG.md"]
hooks:
  PostToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: ./check.sh
---
Read `git log` since the last tag and group the changes.
"""

CLAUDE_IGNORED = (
    "agent",
    "allowed-tools",
    "context",
    "disallowed-tools",
    "effort",
    "hooks",
    "model",
    "paths",
)

# An agentskills.io skill: the spec's optional fields plus a vendor key no
# client knows.
AGENTSKILLS_STYLE = """\
---
name: pdf-processing
description: Extract text and tables from PDF files, fill forms, merge documents.
license: Apache-2.0
compatibility: Requires python3 and poppler-utils
metadata:
  author: example-org
  version: "1.0"
allowed-tools: Bash(python3:*) Read
x-vendor-rating: 5
---
Run `python3 scripts/extract.py <file>`.
"""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws, tmp_path / "home"


def _claude_ws(ws: Path) -> None:
    # The workspace's own `.claude/skills` is read without the global opt-in.
    _write(ws / ".claude" / "skills" / "release-notes" / "SKILL.md", CLAUDE_STYLE)


def _agentskills_ws(ws: Path) -> None:
    _write(ws / ".tst" / "skills" / "pdf-processing" / "SKILL.md", AGENTSKILLS_STYLE)


class TestClaudeStyleSkill:
    def test_loads_and_lists_every_key_it_ignored(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _claude_ws(ws)
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [s.name for s in skills] == ["release-notes"]
        skill = skills[0]
        assert skill.source == "claude_workspace"
        assert skill.ignored_keys == CLAUDE_IGNORED
        assert skill.user_only is False
        assert skill.body.startswith("Read `git log`")

    def test_when_to_use_reaches_the_model(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _claude_ws(ws)
        prompt = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert (
            "- `release-notes`: Write release notes from the merged pull requests. "
            "(when to use: When the user asks for release notes or a changelog entry.)"
        ) in prompt.text

    def test_model_can_load_it(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _claude_ws(ws)
        session = Session(str(ws))
        assert try_load_skill(session, "release-notes", home_dir=home).startswith("Loaded skill")
        prompt = PromptAssembler(ws, home_dir=home).assemble_sync(
            "brain", loaded_skills=session.loaded_skills
        )
        assert "Read `git log` since the last tag" in prompt.text


class TestAgentSkillsStyleSkill:
    def test_loads_and_lists_the_keys_it_ignored(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _agentskills_ws(ws)
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [(s.name, s.ignored_keys) for s in skills] == [
            ("pdf-processing", ("allowed-tools", "x-vendor-rating"))
        ]
        assert skills[0].compatibility == "Requires python3 and poppler-utils"

    def test_compatibility_reaches_the_model(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _agentskills_ws(ws)
        prompt = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert (
            "- `pdf-processing`: Extract text and tables from PDF files, fill forms, "
            "merge documents. (compatibility: Requires python3 and poppler-utils)"
        ) in prompt.text


class TestInspector:
    def test_stack_entries_carry_the_ignored_keys(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _agentskills_ws(ws)
        _write(
            ws / ".tst" / "skills" / "release-notes" / "SKILL.md",
            CLAUDE_STYLE,
        )
        _write(ws / ".tst" / "skills" / "plain" / "SKILL.md", "---\ndescription: Plain\n---\nx\n")
        assembled = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        stack = build_instruction_stack(
            "sess-1", assembled.steering, skills=list_workspace_skills(ws, home)
        )
        by_name = {s.name: s for s in stack.skills}
        assert by_name["release-notes"].ignored_keys == list(CLAUDE_IGNORED)
        assert by_name["pdf-processing"].ignored_keys == ["allowed-tools", "x-vendor-rating"]
        assert by_name["plain"].ignored_keys == []
        wire = json.loads(stack.model_dump_json())
        assert {s["name"]: s["ignored_keys"] for s in wire["skills"]}["plain"] == []


class TestDoctorDetail:
    def test_names_each_skill_with_its_ignored_keys(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _agentskills_ws(ws)
        _write(ws / ".tst" / "skills" / "plain" / "SKILL.md", "---\ndescription: Plain\n---\nx\n")
        detail = skills_doctor_detail(list_workspace_skills(ws, home))
        assert detail == (
            "2 skill(s) found; frontmatter keys ignored: "
            "pdf-processing (allowed-tools, x-vendor-rating)"
        )

    def test_no_skills(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        assert skills_doctor_detail(list_workspace_skills(ws, home)) == "no skills found"


class TestWhenToUseKeys:
    def test_our_key_wins_and_the_other_is_listed(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _write(
            ws / ".tst" / "skills" / "s" / "SKILL.md",
            "---\ndescription: Do\nwhenToUse: Ours\nwhen_to_use: Theirs\n---\nx\n",
        )
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [(s.when_to_use, s.ignored_keys) for s in skills] == [("Ours", ("when_to_use",))]

    def test_blank_falls_back_to_description(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _write(
            ws / ".tst" / "skills" / "s" / "SKILL.md",
            "---\ndescription: Do\nwhen_to_use: '  '\n---\nx\n",
        )
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [(s.when_to_use, s.ignored_keys) for s in skills] == [("Do", ())]
        assert "when to use" not in render_skill_catalog(skills)

    def test_non_string_skips_like_when_to_use_does(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _write(
            ws / ".tst" / "skills" / "s" / "SKILL.md",
            "---\ndescription: Do\nwhen_to_use: [a, b]\n---\nx\n",
        )
        assert SkillDiscoverer(home_dir=home).discover(ws) == []


class TestCompatibilityValues:
    def test_non_string_is_listed_not_shown(self, dirs: tuple[Path, Path]) -> None:
        # TD-5011 loaded any compatibility value; a list must not start skipping.
        ws, home = dirs
        _write(
            ws / ".tst" / "skills" / "s" / "SKILL.md",
            "---\ndescription: Do\ncompatibility: [linux, mac]\n---\nx\n",
        )
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [(s.compatibility, s.ignored_keys) for s in skills] == [("", ("compatibility",))]
        assert "compatibility" not in render_skill_catalog(skills)


class TestDisableModelInvocation:
    @pytest.fixture
    def ws(self, dirs: tuple[Path, Path]) -> Path:
        ws, _home = dirs
        _write(
            ws / ".tst" / "skills" / "deploy" / "SKILL.md",
            "---\ndescription: Deploy to prod\ndisable-model-invocation: true\n---\nShip.\n",
        )
        _write(ws / ".tst" / "skills" / "review" / "SKILL.md", "---\ndescription: Review\n---\nx\n")
        return ws

    def test_loads_as_user_only_and_is_not_listed_as_ignored(
        self, ws: Path, dirs: tuple[Path, Path]
    ) -> None:
        skills = SkillDiscoverer(home_dir=dirs[1]).discover(ws)
        assert [(s.name, s.user_only, s.ignored_keys) for s in skills] == [
            ("deploy", True, ()),
            ("review", False, ()),
        ]
        stack = build_instruction_stack(
            "sess-1",
            PromptAssembler(ws, home_dir=dirs[1]).assemble_sync("brain").steering,
            skills=skills,
        )
        assert {s.name: s.user_only for s in stack.skills} == {"deploy": True, "review": False}

    def test_left_out_of_the_model_catalog(self, ws: Path, dirs: tuple[Path, Path]) -> None:
        prompt = PromptAssembler(ws, home_dir=dirs[1]).assemble_sync("brain")
        assert "- `review`: Review" in prompt.text
        assert "deploy" not in prompt.text

    def test_model_load_is_refused(self, ws: Path, dirs: tuple[Path, Path]) -> None:
        session = Session(str(ws))
        result = try_load_skill(session, "deploy", home_dir=dirs[1])
        assert result.startswith("Error:") and "/deploy" in result
        assert session.loaded_skills == []

    def test_load_skill_tool_is_refused(self, ws: Path, dirs: tuple[Path, Path]) -> None:
        session = Session(str(ws))
        result = asyncio.run(handle_load_skill(session, "deploy"))
        assert result.startswith("Error:")
        assert session.loaded_skills == []

    def test_user_slash_loads_it(self, ws: Path, dirs: tuple[Path, Path]) -> None:
        session = Session(str(ws))
        result = apply_slash_skill(session, "/deploy now", home_dir=dirs[1])
        assert result is not None and result.startswith("Loaded skill `deploy`")
        assert session.loaded_skills == ["deploy"]
        prompt = PromptAssembler(ws, home_dir=dirs[1]).assemble_sync(
            "brain", loaded_skills=session.loaded_skills
        )
        assert "## Skill: deploy" in prompt.text

    @pytest.mark.parametrize("value", ["'yes'", "1"])
    def test_any_value_but_false_is_user_only(self, dirs: tuple[Path, Path], value: str) -> None:
        ws, home = dirs
        _write(
            ws / ".tst" / "skills" / "s" / "SKILL.md",
            f"---\ndescription: Do\ndisable-model-invocation: {value}\n---\nx\n",
        )
        assert [s.user_only for s in SkillDiscoverer(home_dir=home).discover(ws)] == [True]
