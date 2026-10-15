"""A live move to another project rebinds everything the loop built (TD-1715).

The loop wires the dispatcher once, from the root the session opened in:
the path guard, the classifier's boundary, the checkpointer, the memory
committer, the decisions ledger, the approval policy, the shell allowlist
and the prompt assembler. A move between turns must reach all of them, or
the next tool call quietly works in the old project (review finding 17).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from tests.machine_helpers import eventually
from tests.test_artifacts import _open, _shutdown
from tests.test_two_daemons import _Steps
from tstd.autonomy.charter import Charter
from tstd.boundary_config import (
    BoundaryConfig,
    BoundarySection,
    CapsSection,
    load_workspace_boundary,
)
from tstd.daemon import Daemon
from tstd.mock import Script
from tstd.policy import PolicyConfig, load_policy
from tstd.protocol import ToolResult
from tstd.session import SessionRegistry
from tstd.session_lifecycle import move_session
from tstd.session_store import SessionStore

WS2_CONFIG = """\
boundary:
  allowed_commands: [echo]
policy:
  rules:
    - tool: fs_list
      effect: never
"""


class _Recording(_Steps):
    """``_Steps`` that keeps the system prompt of every agent call."""

    def __init__(self, steps: list[Script]) -> None:
        super().__init__(steps)
        self.systems: list[str] = []

    def chat_completion_stream(self, request: Any) -> Any:
        if request.tools:
            self.systems.append(str(request.messages[0].content))
        return super().chat_completion_stream(request)


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return done.stdout


def _repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("ws2\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")


async def test_a_live_move_rebinds_the_whole_working_context(tmp_path: Path) -> None:
    ws, ws2 = tmp_path / "ws", tmp_path / "ws2"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("Old project rule WS1-STEERING-7c1.\n", encoding="utf-8")
    _repo(ws2)
    (ws2 / "AGENTS.md").write_text("New project rule WS2-STEERING-9e4.\n", encoding="utf-8")
    (ws2 / ".tst").mkdir()
    (ws2 / ".tst" / "config.yaml").write_text(WS2_CONFIG, encoding="utf-8")
    steps = [
        Script(
            kind="tool_call",
            tool_name="fs_write",
            tool_arguments=json.dumps({"path": "out.txt", "content": "hi\n"}),
        ),
        Script(kind="tool_call", tool_name="fs_list", tool_arguments='{"path": "."}'),
        Script(kind="tool_call", tool_name="shell", tool_arguments='{"command": "ls"}'),
        Script(kind="stream", content="Done."),
    ]
    provider = _Recording(steps)
    daemon = Daemon(data_dir=tmp_path / "data", provider=provider)
    daemon.skip_all_approvals = True  # policy "never" and the wall still hold
    sid = await _open(daemon, ws)
    try:
        session = daemon.session_registry.get(sid)
        assert session is not None
        move = {"type": "move_session", "session_id": sid, "workspace_path": str(ws2)}
        await daemon._handle_message(json.dumps(move), None)
        message = {"type": "user_message", "session_id": sid, "content": "go"}
        await daemon._handle_message(json.dumps(message), None)
        log = session.event_log

        def results() -> list[ToolResult]:
            return [e for e in log.all_events if isinstance(e, ToolResult)]

        await eventually(lambda: len(results()) == 3, within=20)
        wrote, listed, shelled = results()

        # Guard + classifier: the relative write lands in the new root as a
        # plain in-workspace edit, not a class C write outside the old one.
        assert wrote.status == "success", wrote.output
        assert (ws2 / "out.txt").read_text(encoding="utf-8") == "hi\n"
        assert not (ws / "out.txt").exists()
        # Checkpointer: the edit is committed on the session branch of the new repo.
        branch = f"tst/session/{sid}"
        assert "out.txt" in _git(ws2, "show", "--name-only", "--format=", branch)
        # Ledger: the decision is logged in the new project, not the old one.
        assert "fs_write" in (ws2 / ".tst" / "autonomy" / "DECISIONS.md").read_text("utf-8")
        assert not (ws / ".tst" / "autonomy").exists()
        # Policy: the new project's rule refuses fs_list.
        assert (listed.status, listed.error_code) == ("error", "policy_denied"), listed
        # Shell allowlist: the new project only allows echo.
        assert shelled.status == "error", shelled.output
        assert "allowed_commands" in shelled.output
        # Assembler: the brain is steered by the new project's rules.
        assert provider.systems, "no agent call recorded"
        assert "WS2-STEERING-9e4" in provider.systems[0]
        assert "WS1-STEERING-7c1" not in provider.systems[0]
    finally:
        await _shutdown(daemon, sid)


async def test_an_invalid_target_config_means_defaults_not_the_old_wall(tmp_path: Path) -> None:
    """The move says "defaults — invalid config"; the session must not keep the old wall."""
    origin, target = tmp_path / "origin", tmp_path / "target"
    for root in (origin, target):
        (root / ".tst").mkdir(parents=True)
    (origin / ".tst" / "config.yaml").write_text(WS2_CONFIG, encoding="utf-8")
    (target / ".tst" / "config.yaml").write_text(
        "boundary: [not, a, mapping]\npolicy: just-a-string\n", encoding="utf-8"
    )
    registry = SessionRegistry()
    session = await registry.restore("move-1", str(origin), "running")
    store = SessionStore(tmp_path / "store")
    await store.upsert(session.id, str(origin), "running")
    session.boundary_config = load_workspace_boundary(str(origin))
    session.policy = load_policy(str(origin))
    assert session.boundary_config.boundary.allowed_commands == ["echo"]

    assert await move_session(registry, store, session.id, str(target)) is None

    assert session.boundary_config == BoundaryConfig()
    assert session.policy == PolicyConfig()


async def test_a_moved_charter_run_keeps_the_charter_wall(tmp_path: Path) -> None:
    """Open applies the charter over the workspace config; a move must too."""
    origin, target = tmp_path / "origin", tmp_path / "target"
    origin.mkdir()
    (target / ".tst").mkdir(parents=True)
    (target / ".tst" / "config.yaml").write_text(WS2_CONFIG, encoding="utf-8")
    registry = SessionRegistry()
    session = await registry.restore("move-2", str(origin), "running")
    store = SessionStore(tmp_path / "store")
    await store.upsert(session.id, str(origin), "running")
    wall = BoundarySection(writable_paths=["src/**"], allowed_commands=["git"])
    caps = CapsSection(spend_usd=1.5)
    session.charter = Charter(objective="o", definition_of_done=["d"], boundary=wall, caps=caps)
    rebound: list[str] = []
    session.on_workspace_moved = lambda: rebound.append(session.workspace_path)

    assert await move_session(registry, store, session.id, str(target)) is None

    assert session.boundary_config.boundary == wall
    assert session.boundary_config.caps == caps
    assert rebound == [str(target)]
