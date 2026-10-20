"""A worktree session's workspace config and its trust (TD-5126 review).

A worktree's untracked ``.tst/config.yaml`` is a copy of the project's.
Two review findings: the trust card's decision was saved for the worktree
folder only, so every new session asked again and ``worktree.setup`` never
ran; and Always allow, Revoke, a spend cap and import approvals were saved
into the copy, which goes away with the worktree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.machine_helpers import eventually
from tests.test_artifacts import _open, _shutdown
from tests.test_worktree import git, make_repo, write_config
from tstd.boundary_config import load_workspace_boundary
from tstd.daemon import Daemon
from tstd.loop import _persist_import_approvals
from tstd.policy import approved_imports_from_mapping
from tstd.session import Session
from tstd.workspace_trust import TrustStore, read_view
from tstd.worktree_session import open_worktree

SETUP = 'worktree:\n  setup: "touch setup-ran"\n'
AUTO_MAKE = "policy:\n  rules:\n    - {tool: shell, args: 'make *', effect: auto}\n"


async def _reply(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any] | None:
    raw = await daemon._handle_message(json.dumps(payload), None)
    return None if raw is None else dict(json.loads(raw))


def _live(daemon: Daemon, sid: str) -> Session:
    sess = daemon.session_registry.get(sid)
    assert sess is not None
    return sess


async def test_trust_on_the_card_reaches_the_project_and_its_setup_runs(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    write_config(repo, SETUP)  # untracked: the worktree gets a copy
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    try:
        first = _live(daemon, sid)
        assert first.worktree is not None and first.worktree.setup == "skipped"
        sha = read_view(first.workspace_path, daemon.workspace_trust).sha256
        assert sha is not None

        trusted = await _reply(
            daemon,
            {"type": "trust_workspace_config", "session_id": sid, "sha256": sha, "trust": True},
        )

        assert trusted is None
        assert read_view(repo, daemon.workspace_trust).state == "trusted"
        # The setup that waited for this trust runs in this session's worktree.
        await eventually(lambda: (Path(first.workspace_path) / "setup-ran").exists(), within=10)
        assert first.worktree is not None
        await eventually(lambda: first.worktree is not None and first.worktree.setup == "ok")

        # The next session neither asks again nor skips its setup.
        reply = await _reply(daemon, {"type": "new_session", "session_id": sid})
        assert reply is not None
        second = _live(daemon, reply["session_id"])
        assert second.worktree is not None
        assert read_view(second.workspace_path, daemon.workspace_trust).state == "trusted"
        await eventually(lambda: (Path(second.workspace_path) / "setup-ran").exists(), within=10)
    finally:
        await _shutdown(daemon, sid)


async def test_trust_reaches_only_a_project_config_with_the_same_bytes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    write_config(repo, SETUP)
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    try:
        sess = _live(daemon, sid)
        sha = read_view(sess.workspace_path, daemon.workspace_trust).sha256
        # The project's file changes after the worktree copied it.
        write_config(repo, 'worktree:\n  setup: "touch other"\n')

        await _reply(
            daemon,
            {"type": "trust_workspace_config", "session_id": sid, "sha256": sha, "trust": True},
        )

        assert read_view(sess.workspace_path, daemon.workspace_trust).state == "trusted"
        assert read_view(repo, daemon.workspace_trust).state == "pending"
    finally:
        await _shutdown(daemon, sid)


async def test_a_revoke_and_a_spend_cap_in_a_worktree_session_stay_with_the_project(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    write_config(repo, AUTO_MAKE)
    trust = TrustStore(tmp_path / "data")
    sha = read_view(repo, trust).sha256
    assert sha is not None
    trust.record(repo, sha, "trusted")
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    try:
        sess = _live(daemon, sid)
        assert sess.worktree is not None
        assert [r.args for r in sess.policy.rules] == ["make *"]

        revoked = await _reply(
            daemon,
            {"type": "revoke_policy_rule", "session_id": sid, "tool": "shell", "args": "make *"},
        )
        capped = await _reply(daemon, {"type": "set_spend_cap", "session_id": sid, "spend_usd": 80})

        assert revoked is not None and revoked["rules"] == []
        assert capped is None
        assert sess.policy.rules == []
        assert sess.boundary_config.caps.spend_usd == 80
        # Saved in the project's file, where the next session reads it.
        assert "make *" not in (repo / ".tst" / "config.yaml").read_text(encoding="utf-8")
        assert load_workspace_boundary(repo).caps.spend_usd == 80
        copy = Path(sess.workspace_path) / ".tst" / "config.yaml"
        assert copy.read_bytes() == (repo / ".tst" / "config.yaml").read_bytes()

        # The worktree holds no work, so it goes; the project keeps both.
        listed = await _reply(daemon, {"type": "close_worktree", "session_id": sid})
        assert listed is not None and listed["type"] == "session_list"
        later = _live(daemon, await _open(daemon, repo))
        assert later.worktree is not None
        assert later.policy.rules == []
        assert later.boundary_config.caps.spend_usd == 80
    finally:
        await _shutdown(daemon, sid)


async def test_a_spend_cap_in_a_project_with_no_config_is_saved_in_the_project(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    try:
        sess = _live(daemon, sid)
        assert sess.worktree is not None
        assert (
            await _reply(daemon, {"type": "set_spend_cap", "session_id": sid, "spend_usd": 70})
            is None
        )
        assert load_workspace_boundary(repo).caps.spend_usd == 70
        assert sess.boundary_config.caps.spend_usd == 70
    finally:
        await _shutdown(daemon, sid)


async def test_import_approvals_in_a_worktree_session_are_saved_in_the_project(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    write_config(repo, "# local settings\n")
    trust = TrustStore(tmp_path / "data")
    made = await open_worktree(str(repo), "imp1", folder=tmp_path / "wts", trust=trust)
    assert made.worktree is not None
    sess = Session(str(made.worktree.root))
    sess.worktree = made.worktree
    sess.trust_store = trust
    shared = (tmp_path / "shared.md").resolve()

    _persist_import_approvals(sess, {shared})

    saved = read_view(repo, trust)
    assert approved_imports_from_mapping(saved.path, saved.effective) == {shared}
    copy = made.worktree.root / ".tst" / "config.yaml"
    assert copy.read_bytes() == (repo / ".tst" / "config.yaml").read_bytes()


async def test_a_tracked_config_is_the_branchs_own_file(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    write_config(repo, "# shared settings\n")
    git(repo, "add", ".tst/config.yaml")
    git(repo, "commit", "-qm", "config")
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    try:
        sess = _live(daemon, sid)
        assert sess.worktree is not None and sess.worktree.config_tracked
        assert (
            await _reply(daemon, {"type": "set_spend_cap", "session_id": sid, "spend_usd": 60})
            is None
        )
        # A tracked file changes on the branch, as a change the worktree carries.
        assert load_workspace_boundary(sess.workspace_path).caps.spend_usd == 60
        assert (repo / ".tst" / "config.yaml").read_text(encoding="utf-8") == "# shared settings\n"
        asked = await _reply(daemon, {"type": "close_worktree", "session_id": sid})
        assert asked is not None and asked["code"] == "worktree_has_changes"
    finally:
        await _shutdown(daemon, sid)
