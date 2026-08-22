"""Slash-command discovery, expansion, and wire listing (TD-4501).

The loader tests use isolated home/workspace trees so they never read a
real ``~/.tstdesk``. The wire test starts the daemon on a loopback port,
like the MCP wiring tests.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.context.commands import (
    discover_commands,
    expand_command,
    global_commands_dir,
    read_command_body,
)
from tstd.daemon import Daemon
from tstd.logging import user_data_dir
from tstd.protocol import PROTOCOL_VERSION


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """An isolated home so ~/.tstdesk lookups never see the real one."""
    return tmp_path / "home"


def _plant(root: Path, name: str, text: str = "body\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


class TestDiscoverCommands:
    def test_workspace_tree_discovered(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _plant(ws / ".tst" / "commands", "deploy.md")
        found = discover_commands(ws)
        assert [(c.name, c.source, c.fallback) for c in found] == [("deploy", "workspace", False)]

    def test_user_global_discovered_and_wins_names(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        _plant(ws / ".tst" / "commands", "deploy.md", "workspace body\n")
        _plant(global_commands_dir(home), "deploy.md", "user body\n")
        found = discover_commands(ws, home_dir=home)
        # User-global wins a shared name — the inverse of steering.
        assert len(found) == 1
        assert (found[0].source, found[0].name) == ("user", "deploy")

    def test_claude_fallback_when_ours_empty(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        ours = _plant(ws / ".claude" / "commands", "review.md")
        theirs = _plant(home / ".claude" / "commands", "ship.md")
        found = discover_commands(ws, home_dir=home)
        by_name = {c.name: c for c in found}
        assert by_name["review"].fallback is True
        assert by_name["review"].path == ours.resolve()
        assert by_name["ship"].fallback is True
        assert by_name["ship"].path == theirs.resolve()

    def test_fallback_shadowed_once_ours_exist(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        _plant(ws / ".tst" / "commands", "review.md")
        _plant(ws / ".claude" / "commands", "review.md", "claude copy\n")
        found = discover_commands(ws, home_dir=home)
        assert len(found) == 1
        assert found[0].fallback is False

    def test_unsafe_stems_not_offered(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        commands = ws / ".tst" / "commands"
        _plant(commands, "my cmd.md")  # space — unspellable after /
        _plant(commands, "notes.md.bak")  # not .md at the stem level
        _plant(commands, "-leading.md")  # looks like a flag, still spellable
        assert [c.name for c in discover_commands(ws)] == ["-leading"]

    def test_symlink_escape_skipped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        outside = tmp_path / "outside"
        outside.mkdir()
        _plant(outside, "evil.md")
        _plant(ws / ".tst" / "commands", "real.md")
        (ws / ".tst" / "commands" / "sneaky.md").symlink_to(outside / "evil.md")
        assert [c.name for c in discover_commands(ws)] == ["real"]

    def test_symlinked_root_is_skipped(self, tmp_path: Path) -> None:
        # A symlinked root relocates every candidate before containment
        # checks run (TD-4502 convergence) — fail closed on the root itself.
        ws = tmp_path / "ws"
        real = tmp_path / "elsewhere"
        _plant(real, "evil.md")
        root = ws / ".tst" / "commands"
        root.parent.mkdir(parents=True)
        root.symlink_to(real)
        assert discover_commands(ws) == []

    def test_missing_everything_is_empty(self, tmp_path: Path, home: Path) -> None:
        assert discover_commands(tmp_path / "nope", home_dir=home) == []


class TestExpandCommand:
    def test_frontmatter_stripped(self, tmp_path: Path) -> None:
        path = _plant(
            tmp_path,
            "deploy.md",
            "---\ndescription: ship it\n---\nRun the deploy.\n",
        )
        assert read_command_body(path).strip() == "Run the deploy."

    def test_args_appended_only_when_present(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        file = _plant(ws / ".tst" / "commands", "deploy.md", "Deploy steps.\n")
        command = discover_commands(ws)[0]
        with_args = expand_command(command, "prod --dry-run")
        assert f"/{command.name}" in with_args and "Deploy steps." in with_args
        assert "prod --dry-run" in with_args
        bare = expand_command(command, None)
        assert "arguments" not in bare.lower()
        assert str(file) in with_args  # provenance names the file


class TestDaemonSplice:
    @pytest.mark.asyncio
    async def test_invocation_expands_and_unknown_delivers_verbatim(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _plant(ws / ".tst" / "commands", "deploy.md", "---\nhidden: true\n---\nShip it.\n")
        daemon = Daemon(data_dir=tmp_path / "data")
        # TD-4502: expansion resolves against the session (skills record
        # loads there); tests stand in a session-shaped namespace.
        sess = SimpleNamespace(workspace_path=str(ws), loaded_skills={})
        expanded = await daemon._expand_slash(sess, "/deploy prod now")  # type: ignore[arg-type]
        assert "Ship it." in expanded
        assert "prod now" in expanded
        assert "hidden" not in expanded
        # Not a command or skill: verbatim, slash and all.
        assert (
            await daemon._expand_slash(sess, "/nope args") == "/nope args"  # type: ignore[arg-type]
        )
        assert (
            await daemon._expand_slash(sess, "plain question?") == "plain question?"  # type: ignore[arg-type]
        )


async def _start_daemon(tmp: str) -> tuple[Daemon, asyncio.Task[Any]]:
    daemon = Daemon(data_dir=Path(tmp))
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


class TestWire:
    @pytest.mark.asyncio
    async def test_list_commands_replies_connection_scoped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _plant(ws / ".tst" / "commands", "deploy.md")
        config_dir = user_data_dir()
        config_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as daemon_tmp:
            daemon, task = await _start_daemon(daemon_tmp)
            try:
                wsock = await connect(f"ws://127.0.0.1:{daemon.ws_server.port}")
                await wsock.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "token": daemon.ws_server.token,
                            "version": PROTOCOL_VERSION,
                        }
                    )
                )
                ack = json.loads(await asyncio.wait_for(wsock.recv(), timeout=2))
                assert ack["type"] == "hello_ack"

                await wsock.send(json.dumps({"type": "list_commands", "workspace_path": str(ws)}))
                reply = json.loads(await asyncio.wait_for(wsock.recv(), timeout=2))
                assert reply["type"] == "commands"
                assert reply["seq"] == 1
                assert reply["workspace_path"] == str(ws)
                assert [(c["name"], c["source"]) for c in reply["commands"]] == [
                    ("deploy", "workspace")
                ]
                await wsock.close()
            finally:
                daemon._shutdown_event.set()
                await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_unknown_workspace_is_typed_error(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as daemon_tmp:
            daemon, task = await _start_daemon(daemon_tmp)
            try:
                wsock = await connect(f"ws://127.0.0.1:{daemon.ws_server.port}")
                await wsock.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "token": daemon.ws_server.token,
                            "version": PROTOCOL_VERSION,
                        }
                    )
                )
                await asyncio.wait_for(wsock.recv(), timeout=2)
                await wsock.send(
                    json.dumps(
                        {
                            "type": "list_commands",
                            "workspace_path": str(tmp_path / "missing"),
                        }
                    )
                )
                reply = json.loads(await asyncio.wait_for(wsock.recv(), timeout=2))
                assert reply["type"] == "error"
                assert reply["code"] == "workspace_not_found"
                await wsock.close()
            finally:
                daemon._shutdown_event.set()
                await asyncio.gather(task, return_exceptions=True)
