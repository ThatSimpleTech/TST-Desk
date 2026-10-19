"""The daemon's own git never runs a repo's fsmonitor command (TD-5108).

Found by the TD-5141 review: one Class A ``fs_write`` to ``.git/config``
that set ``core.fsmonitor`` ran that command, with no approval and outside
the wall, the next time the checkpointer ran ``git add`` after a write.
Every git call the daemon makes now starts with ``git_command``.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import tstd
from tstd.autonomy import Checkpointer
from tstd.autonomy.checkpoint import DecisionClass
from tstd.context.manifest import WorkspaceManifest
from tstd.git_safe import GIT_SAFE_CONFIG, git_command
from tstd.memory_commit import MemoryCommitter

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
needs_posix = pytest.mark.skipif(
    os.name == "nt", reason="the planted command is a POSIX shell command"
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo_with_planted_fsmonitor(tmp_path: Path) -> tuple[Path, Path]:
    """A repo whose ``.git/config`` names a command that touches *marker*."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "baseline")
    marker = tmp_path / "fsmonitor-ran"
    # Planted after the baseline so only the daemon's calls can trip it.
    _git(repo, "config", "core.fsmonitor", f"touch {marker}; false")
    return repo, marker


@needs_git
@needs_posix
def test_planted_fsmonitor_runs_for_plain_git(tmp_path: Path) -> None:
    """Control: the planted config is live, so the tests below mean something."""
    repo, marker = _repo_with_planted_fsmonitor(tmp_path)
    (repo / "b.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True)
    assert marker.exists()


@needs_git
@needs_posix
async def test_checkpoint_does_not_run_a_planted_fsmonitor(tmp_path: Path) -> None:
    repo, marker = _repo_with_planted_fsmonitor(tmp_path)
    (repo / "b.txt").write_text("written by the agent\n")

    outcome = await Checkpointer(repo, "s1").checkpoint(
        [repo / "b.txt"],
        tool_name="fs_write",
        tool_call_id="tc-1",
        decision_class=DecisionClass.A,
        decision_rule="in-workspace-edit",
    )

    assert outcome.status == "committed"
    assert not marker.exists()


@needs_git
@needs_posix
async def test_memory_commit_does_not_run_a_planted_fsmonitor(tmp_path: Path) -> None:
    repo, marker = _repo_with_planted_fsmonitor(tmp_path)
    note = repo / ".tst" / "memory" / "gotchas.md"
    note.parent.mkdir(parents=True)
    note.write_text("- a gotcha\n")

    outcome = await MemoryCommitter(repo).commit([note])

    assert outcome.status == "committed"
    assert not marker.exists()


@needs_git
@needs_posix
def test_manifest_does_not_run_a_planted_fsmonitor(tmp_path: Path) -> None:
    repo, marker = _repo_with_planted_fsmonitor(tmp_path)

    result = WorkspaceManifest().build(repo)

    assert "a.txt" in result.text
    assert not marker.exists()


def test_git_command_pins_fsmonitor_off_before_the_subcommand() -> None:
    argv = git_command("-C", "/ws", "status")
    assert argv[0] == "git"
    assert argv[1 : 1 + len(GIT_SAFE_CONFIG)] == list(GIT_SAFE_CONFIG)
    assert "core.fsmonitor=false" in GIT_SAFE_CONFIG
    assert argv[-3:] == ["-C", "/ws", "status"]


# Developer harnesses, not daemon code: they drive git on fixtures they made.
_HARNESSES = frozenset(
    {"benchmarks.py", "e2e_checks.py", "e2e_harness.py", "e2e_m9.py", "e2e_memory_checks.py"}
)
_SPAWNERS = frozenset(
    {"create_subprocess_exec", "run", "Popen", "check_output", "check_call", "call"}
)


def _raw_git_sites(source: str) -> list[int]:
    """Lines that spawn ``git`` from a literal instead of ``git_command``."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.List, ast.Tuple)):
            first = node.elts[0] if node.elts else None
            if isinstance(first, ast.Constant) and first.value == "git":
                lines.append(node.lineno)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0]
            if name in _SPAWNERS and isinstance(first, ast.Constant) and first.value == "git":
                lines.append(node.lineno)
    return sorted(lines)


def test_every_daemon_git_call_goes_through_git_command() -> None:
    package = Path(tstd.__file__).parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path.name in _HARNESSES or path.name == "git_safe.py":
            continue
        for line in _raw_git_sites(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(package)}:{line}")
    assert offenders == []


def test_the_scan_catches_a_raw_git_call() -> None:
    sample = (
        "import asyncio, subprocess\n"
        "subprocess.run(['git', 'status'])\n"
        "asyncio.create_subprocess_exec('git', '-C', '.', 'add')\n"
    )
    assert _raw_git_sites(sample) == [2, 3]
