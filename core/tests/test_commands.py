"""Slash-command discovery, protocol, and Class C writes (TD-4501)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier, DecisionRequest
from tstd.autonomy.classifier import is_steering_write
from tstd.context.assembler import ContextAssembler
from tstd.context.commands import (
    COMMAND_BODY_CAP,
    CommandDiscoverer,
    list_workspace_commands,
    list_workspace_commands_async,
)
from tstd.context.discover import SteeringFileResolver
from tstd.context.prompt import PromptAssembler
from tstd.daemon import Daemon
from tstd.tools.boundary import PathGuard, RefusalError


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _discover(ws: Path, home: Path) -> list[str]:
    return [c.name for c in CommandDiscoverer(home_dir=home).discover(ws)]


class TestDiscovery:
    def test_workspace_only_command_appears(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / ".tst" / "commands" / "review.md", "Please review.\n")
        commands = CommandDiscoverer(home_dir=home).discover(ws)
        assert [c.name for c in commands] == ["review"]
        assert commands[0].body == "Please review.\n"
        assert commands[0].source == "workspace"
        assert commands[0].description == ""
        assert commands[0].too_large is False

    def test_user_global_wins_on_name(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / ".tst" / "commands" / "review.md", "workspace body\n")
        _write(home / ".tstdesk" / "commands" / "review.md", "user body\n")
        commands = CommandDiscoverer(home_dir=home).discover(ws)
        assert len(commands) == 1
        assert commands[0].name == "review"
        assert commands[0].body == "user body\n"
        assert commands[0].source == "user"

    def test_workspace_and_user_distinct_names(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / ".tst" / "commands" / "review.md", "ws\n")
        _write(home / ".tstdesk" / "commands" / "ship.md", "user\n")
        assert _discover(ws, home) == ["review", "ship"]

    def test_fallback_claude_only_when_ours_empty(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / ".claude" / "commands" / "review.md", "claude body\n")
        commands = CommandDiscoverer(home_dir=home).discover(ws)
        assert [c.name for c in commands] == ["review"]
        assert commands[0].source == "claude_workspace"
        assert commands[0].body == "claude body\n"

    def test_fallback_not_used_when_ours_has_any_md(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / ".tst" / "commands" / "ship.md", "ours\n")
        _write(ws / ".claude" / "commands" / "review.md", "claude\n")
        assert _discover(ws, home) == ["ship"]

    def test_fallback_not_used_when_user_global_has_md(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(home / ".tstdesk" / "commands" / "ship.md", "ours\n")
        _write(ws / ".claude" / "commands" / "review.md", "claude\n")
        assert _discover(ws, home) == ["ship"]

    def test_fallback_includes_user_claude_and_user_wins(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / ".claude" / "commands" / "review.md", "ws claude\n")
        _write(home / ".claude" / "commands" / "review.md", "user claude\n")
        commands = CommandDiscoverer(home_dir=home).discover(ws)
        assert len(commands) == 1
        assert commands[0].body == "user claude\n"
        assert commands[0].source == "claude_user"

    def test_ignores_non_md_and_invalid_stems(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        d = ws / ".tst" / "commands"
        _write(d / "review.md", "ok\n")
        _write(d / "notes.txt", "no\n")
        _write(d / "bad name.md", "no\n")
        assert _discover(ws, home) == ["review"]

    def test_frontmatter_description_extra_keys_ignored(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(
            ws / ".tst" / "commands" / "review.md",
            "---\ndescription: Review the diff\nargument-hint: files\n---\nDo the review.\n",
        )
        commands = CommandDiscoverer(home_dir=home).discover(ws)
        assert commands[0].description == "Review the diff"
        assert commands[0].body == "Do the review.\n"

    def test_too_large_body_is_omitted(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        huge = "x" * (COMMAND_BODY_CAP + 1)
        _write(ws / ".tst" / "commands" / "huge.md", huge)
        commands = CommandDiscoverer(home_dir=home).discover(ws)
        assert commands[0].too_large is True
        assert commands[0].body == ""

    def test_empty_trees_list_nothing(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        ws.mkdir()
        assert list_workspace_commands(ws, home_dir=home) == []

    async def test_async_wrapper_matches_sync(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / ".tst" / "commands" / "review.md", "body\n")
        sync = list_workspace_commands(ws, home_dir=home)
        async_listed = await list_workspace_commands_async(ws, home_dir=home)
        assert [c.name for c in async_listed] == [c.name for c in sync]


class TestNotSteering:
    def test_command_body_absent_from_assemble_and_prefix(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        marker = "SLASH-COMMAND-BODY-MUST-NOT-APPEAR"
        _write(ws / "AGENTS.md", "workspace steering\n")
        _write(ws / ".tst" / "commands" / "review.md", f"{marker}\n")
        assembled = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(ws)
        assert marker not in assembled.block
        assert all("commands" not in s.path.as_posix() for s in assembled.sources)

        prompt = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert marker not in prompt.text
        assert marker not in prompt.prefix
        assert "workspace steering" in prompt.prefix


class TestClassC:
    def test_tst_commands_write_is_class_c_and_guard_refuses(self, tmp_path: Path) -> None:
        target = tmp_path / ".tst" / "commands" / "foo.md"
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

    def test_claude_commands_write_is_class_c(self, tmp_path: Path) -> None:
        target = tmp_path / ".claude" / "commands" / "foo.md"
        b = Boundary(workspace_root=tmp_path, writable_patterns=("**",))
        assert is_steering_write(b, target)
        with pytest.raises(RefusalError) as ei:
            PathGuard(b).check_write(target)
        assert ei.value.code == "steering_file"

    def test_user_global_commands_dir_refused_in_workspace(self, tmp_path: Path) -> None:
        # Writable_paths hole: workspace is home, so ~/.tstdesk/commands is inside.
        home = tmp_path / "home"
        target = home / ".tstdesk" / "commands" / "foo.md"
        b = Boundary(workspace_root=home, writable_patterns=("**",))
        assert is_steering_write(b, target)
        with pytest.raises(RefusalError) as ei:
            PathGuard(b).check_write(target)
        assert ei.value.code == "steering_file"

    def test_ordinary_file_is_not_a_command_write(self, tmp_path: Path) -> None:
        b = Boundary(workspace_root=tmp_path, writable_patterns=("**",))
        assert not is_steering_write(b, tmp_path / "notes.md")
        assert not is_steering_write(b, tmp_path / ".tst" / "memory" / "gotchas.md")


class TestDaemon:
    async def test_list_commands_returns_expected_names(self, tmp_path: Path) -> None:
        _write(tmp_path / ".tst" / "commands" / "review.md", "Review this.\n")
        _write(
            tmp_path / ".tst" / "commands" / "ship.md",
            "---\ndescription: Ship it\n---\nShip.\n",
        )
        daemon = Daemon(data_dir=tmp_path / "data")
        listed = await daemon._handle_message(
            json.dumps({"type": "list_commands", "workspace_path": str(tmp_path)}),
            None,
        )
        assert listed is not None
        payload = json.loads(listed)
        assert payload["type"] == "command_list"
        names = [c["name"] for c in payload["commands"]]
        assert names == ["review", "ship"]
        by_name = {c["name"]: c for c in payload["commands"]}
        assert by_name["review"]["body"] == "Review this.\n"
        assert by_name["ship"]["description"] == "Ship it"
        assert by_name["ship"]["source"] == "workspace"
        await daemon._shutdown()

    async def test_missing_workspace_is_typed_error(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        reply = await daemon._handle_message(
            json.dumps(
                {
                    "type": "list_commands",
                    "workspace_path": str(tmp_path / "nope"),
                }
            ),
            None,
        )
        assert reply is not None
        assert json.loads(reply)["code"] == "workspace_not_found"
        await daemon._shutdown()
