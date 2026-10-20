"""``tst run`` says when its session works in a git worktree (TD-5126 review).

A run in a git repository edits a worktree under the data dir, not the
folder it named. The finding: nothing in the CLI's output said so, so a
script saw a successful reply and no change in ``--workspace``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_tst_run_scripts import _Echo, _json_line, _start, _stop
from tests.test_worktree import make_repo
from tstd import cli_run
from tstd.cli_turn import EXIT_OK


async def test_a_run_in_a_repo_names_its_worktree_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    repo = make_repo(tmp_path / "repo").resolve()
    daemon, task = await _start(data_dir, _Echo())
    try:
        code = await cli_run.run_turn(repo, "hi", data_dir, output="json")
        first = capsys.readouterr()
        sessions = await daemon.session_registry.list_sessions()
        assert len(sessions) == 1
        sess = sessions[0]
        assert sess.worktree is not None
        again = await cli_run.run_turn(repo, "again", data_dir, continue_last=True)
        second = capsys.readouterr()
    finally:
        await _stop(daemon, task)

    assert code == EXIT_OK and again == EXIT_OK
    # stdout is still the one documented object.
    result = _json_line(first.out)
    assert set(result) == {"reply", "session_id", "cost", "status", "error"}
    for err in (first.err, second.err):
        assert f"tst/wt/{sess.id[:8]}" in err
        assert sess.workspace_path in err
        assert str(repo) in err
    assert "echo: again" in second.out


async def test_a_run_outside_git_prints_no_worktree_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    plain = (tmp_path / "plain").resolve()
    plain.mkdir()
    daemon, task = await _start(data_dir, _Echo())
    try:
        code = await cli_run.run_turn(plain, "hi", data_dir, output="ndjson")
    finally:
        await _stop(daemon, task)
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "worktree" not in captured.err
    types = [json.loads(line)["type"] for line in captured.out.splitlines()]
    assert "worktree_state" not in types
