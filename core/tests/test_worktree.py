"""A git worktree per session (TD-5126): making, filling and closing one."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from tstd.boundary_config import load_workspace_boundary
from tstd.session import Session
from tstd.tools.shell import sanitized_env
from tstd.workspace_trust import TrustStore, find_loosened, read_view
from tstd.worktree import SessionWorktree, WorktreeError, changes
from tstd.worktree_session import (
    WorktreeHasChanges,
    close,
    open_worktree,
    start_setup,
    workspace_settings,
)


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return done.stdout


def on_disk(path: str) -> bool:
    return Path(path).exists()


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\nbuild/\nnode_modules/\n", encoding="utf-8")
    git(root, "add", "README.md", ".gitignore")
    git(root, "commit", "-q", "-m", "init")
    return root


def write_config(root: Path, text: str) -> None:
    (root / ".tst").mkdir(exist_ok=True)
    (root / ".tst" / "config.yaml").write_text(text, encoding="utf-8")


async def opened(repo: Path, tmp_path: Path, trust: TrustStore | None = None) -> SessionWorktree:
    made = await open_worktree(str(repo), "abcd1234", folder=tmp_path / "wts", trust=trust)
    assert made.worktree is not None, made.note
    return made.worktree


async def test_a_git_workspace_gets_a_worktree_on_a_new_branch_from_head(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    head = git(repo, "rev-parse", "HEAD").strip()

    wt = await opened(repo, tmp_path)

    assert Path(wt.path) == tmp_path / "wts" / "repo-abcd1234"
    assert wt.branch == "tst/wt/abcd1234"
    assert wt.base == head
    assert wt.project == str(repo)
    assert (Path(wt.path) / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert git(Path(wt.path), "rev-parse", "--abbrev-ref", "HEAD").strip() == wt.branch
    assert git(Path(wt.path), "rev-parse", "HEAD").strip() == head
    # The main checkout is untouched: still on main, no new files.
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(repo, "status", "--porcelain") == ""


async def test_a_subfolder_workspace_works_in_the_same_subfolder_of_the_worktree(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "app").mkdir()
    (repo / "app" / "main.py").write_text("print(1)\n", encoding="utf-8")
    git(repo, "add", "app")
    git(repo, "commit", "-q", "-m", "app")

    made = await open_worktree(str(repo / "app"), "sub1", folder=tmp_path / "wts", trust=None)

    assert made.worktree is not None
    assert made.worktree.root == tmp_path / "wts" / "repo-sub1" / "app"
    assert (made.worktree.root / "main.py").is_file()


async def test_a_folder_the_last_commit_lacks_gets_no_worktree_and_says_so(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "projects" / "app").mkdir(parents=True)
    (repo / "projects" / "app" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (repo / "build").mkdir()  # ignored by the repo's .gitignore
    (repo / "build" / "notes.txt").write_text("scratch\n", encoding="utf-8")

    for folder in (repo / "projects" / "app", repo / "build"):
        made = await open_worktree(str(folder), "new1", folder=tmp_path / "wts", trust=None)
        # A worktree would hold none of the folder's files: work in the folder.
        assert made.worktree is None
        assert "not in the last commit" in made.note and str(folder) in made.note
    assert not (tmp_path / "wts").exists()
    assert git(repo, "branch", "--list", "tst/wt/*") == ""


async def test_no_worktree_outside_git_or_before_the_first_commit(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")

    for folder in (plain, empty):
        made = await open_worktree(str(folder), "x1", folder=tmp_path / "wts", trust=None)
        assert made.worktree is None
        assert made.note == ""
    assert not (tmp_path / "wts").exists()


async def test_one_workspace_key_turns_it_off(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    # Even from a file that is not trusted: turning it off loosens nothing.
    write_config(
        repo, "worktree:\n  enabled: false\npolicy:\n  rules:\n    - {tool: shell, effect: auto}\n"
    )
    trust = TrustStore(tmp_path / "data")
    assert read_view(repo, trust).state == "pending"

    made = await open_worktree(str(repo), "off1", folder=tmp_path / "wts", trust=trust)

    assert made.worktree is None
    assert workspace_settings(str(repo), trust).enabled is False


async def test_uncommitted_changes_in_the_main_checkout_are_counted(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    # TST Desk's own scaffolding is not the user's change.
    write_config(repo, "# comments only\n")

    wt = await opened(repo, tmp_path)

    assert wt.main_dirty == 2
    # The worktree has the committed text, not the edit.
    assert (Path(wt.path) / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert not (Path(wt.path) / "new.txt").exists()


async def test_worktree_include_copies_only_ignored_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / ".env").write_text("TOKEN=local\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("built\n", encoding="utf-8")
    (repo / "notes.txt").write_text("untracked, not ignored\n", encoding="utf-8")
    (repo / ".tst").mkdir()
    (repo / ".tst" / "worktree-include").write_text(
        ".env\nnotes.txt\nREADME.md\n", encoding="utf-8"
    )

    wt = await opened(repo, tmp_path)

    top = Path(wt.path)
    assert (top / ".env").read_text(encoding="utf-8") == "TOKEN=local\n"
    # Not listed: stays behind. Listed but not ignored: never copied.
    assert not (top / "build").exists()
    assert not (top / "notes.txt").exists()
    assert (top / "README.md").read_text(encoding="utf-8") == "hello\n"


async def test_an_untracked_workspace_config_and_its_trust_come_along(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".env\n.tst/config.yaml\n", encoding="utf-8")
    git(repo, "commit", "-qam", "ignore config")
    config = "policy:\n  rules:\n    - {tool: shell, args: 'make *', effect: auto}\n"
    write_config(repo, config)
    trust = TrustStore(tmp_path / "data")
    view = read_view(repo, trust)
    assert view.sha256 is not None
    trust.record(repo, view.sha256, "trusted")

    wt = await opened(repo, tmp_path, trust)

    copied = wt.root / ".tst" / "config.yaml"
    assert copied.read_text(encoding="utf-8") == config
    assert read_view(wt.root, trust).state == "trusted"


async def test_setup_runs_once_from_a_trusted_config_and_the_first_turn_waits(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    write_config(repo, 'worktree:\n  setup: "sleep 0.3; echo ready > built.txt"\n')
    trust = TrustStore(tmp_path / "data")
    view = read_view(repo, trust)
    assert view.state == "pending"  # a repo-shipped command is loosening
    assert view.sha256 is not None
    trust.record(repo, view.sha256, "trusted")

    made = await open_worktree(str(repo), "setup1", folder=tmp_path / "wts", trust=trust)
    assert made.worktree is not None
    assert made.setup.startswith("sleep")
    sess = Session(str(made.worktree.root))
    sess.worktree = made.worktree
    done = asyncio.Event()

    async def on_done(_s: Session) -> None:
        done.set()

    task = start_setup(sess, made.setup, 30.0, on_done)
    await sess.add_user_message("go")
    # The message waits while setup runs.
    waiting = asyncio.create_task(sess.wait_for_user_message())
    await asyncio.sleep(0.1)
    assert not waiting.done()
    await asyncio.wait_for(done.wait(), 10)
    taken = await asyncio.wait_for(waiting, 5)
    await task

    assert taken is not None
    assert made.worktree.setup == "ok"
    assert (made.worktree.root / "built.txt").read_text(encoding="utf-8").strip() == "ready"


async def test_setup_from_an_untrusted_config_is_skipped(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    write_config(repo, 'worktree:\n  setup: "touch pwned"\n')
    trust = TrustStore(tmp_path / "data")

    made = await open_worktree(str(repo), "skip1", folder=tmp_path / "wts", trust=trust)

    assert made.worktree is not None
    assert made.setup == ""
    assert made.worktree.setup == "skipped"
    assert "not trusted" in made.worktree.setup_note
    assert not (made.worktree.root / "pwned").exists()


def test_setup_is_a_loosened_setting_and_parses(tmp_path: Path) -> None:
    path = tmp_path / ".tst" / "config.yaml"
    found = find_loosened(path, {"worktree": {"setup": "npm ci"}})
    assert [(f.key, f.value) for f in found] == [("worktree.setup", "npm ci")]
    assert find_loosened(path, {"worktree": {"enabled": False}}) == []
    write_config(tmp_path, "worktree:\n  enabled: false\n  setup: npm ci\n")
    loaded = load_workspace_boundary(tmp_path)
    assert (loaded.worktree.enabled, loaded.worktree.setup) == (False, "npm ci")


async def test_close_removes_an_unchanged_worktree_whatever_was_asked(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    wt = await opened(repo, tmp_path)

    note = await close(wt, "keep", title="t")

    assert "no changes" in note
    assert not on_disk(wt.path)
    assert git(repo, "branch", "--list", wt.branch) == ""


async def test_tst_desk_files_are_not_changes_but_a_new_rule_is(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    wt = await opened(repo, tmp_path)
    top = Path(wt.path)
    for planted in (".tst/memory/notes.md", ".tst/autonomy/DECISIONS.md", ".tst/config.yaml"):
        (top / planted).parent.mkdir(parents=True, exist_ok=True)
        (top / planted).write_text("planted\n", encoding="utf-8")
    assert not (await changes(wt, sanitized_env())).changed

    (top / ".tst" / "rules").mkdir()
    (top / ".tst" / "rules" / "style.md").write_text("Use tabs.\n", encoding="utf-8")
    assert (await changes(wt, sanitized_env())).files == 1

    await close(wt, "merge", title="Add a rule")
    assert (repo / ".tst" / "rules" / "style.md").read_text(encoding="utf-8") == "Use tabs.\n"
    assert not (repo / ".tst" / "autonomy").exists()


async def test_close_with_changes_asks_then_keeps(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    wt = await opened(repo, tmp_path)
    (Path(wt.path) / "feature.txt").write_text("work\n", encoding="utf-8")

    with pytest.raises(WorktreeHasChanges, match="1 changed file"):
        await close(wt, None, title="t")
    assert (Path(wt.path) / "feature.txt").exists()

    note = await close(wt, "keep", title="t")
    assert "Kept" in note
    assert (Path(wt.path) / "feature.txt").exists()
    assert wt.branch in git(repo, "branch", "--list", wt.branch)


async def test_close_merge_back_commits_and_merges_into_the_main_checkout(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    wt = await opened(repo, tmp_path)
    (Path(wt.path) / "feature.txt").write_text("work\n", encoding="utf-8")
    (Path(wt.path) / "README.md").write_text("hello\nmore\n", encoding="utf-8")
    # The user's own unrelated edit in the main checkout survives the merge.
    (repo / "scratch.txt").write_text("mine\n", encoding="utf-8")

    note = await close(wt, "merge", title="Add a feature")

    assert "Merged" in note
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "work\n"
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello\nmore\n"
    assert (repo / "scratch.txt").read_text(encoding="utf-8") == "mine\n"
    assert "Add a feature" in git(repo, "log", "--format=%s", "-n", "3")
    assert git(repo, "status", "--porcelain") == "?? scratch.txt\n"
    assert not on_disk(wt.path)
    assert git(repo, "branch", "--list", wt.branch) == ""


async def test_merge_back_refuses_a_conflict_and_leaves_both_sides_alone(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    wt = await opened(repo, tmp_path)
    (Path(wt.path) / "README.md").write_text("theirs\n", encoding="utf-8")
    (repo / "README.md").write_text("ours\n", encoding="utf-8")
    git(repo, "commit", "-qam", "ours")

    with pytest.raises(WorktreeError, match="conflict"):
        await close(wt, "merge", title="t")

    assert (repo / "README.md").read_text(encoding="utf-8") == "ours\n"
    assert git(repo, "status", "--porcelain") == ""
    assert on_disk(wt.path)
    # The worktree's edit was committed on its branch, not lost.
    assert git(repo, "show", f"{wt.branch}:README.md") == "theirs\n"


async def test_close_remove_discards_the_changes_and_the_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    wt = await opened(repo, tmp_path)
    (Path(wt.path) / "feature.txt").write_text("work\n", encoding="utf-8")
    git(Path(wt.path), "add", "feature.txt")
    git(Path(wt.path), "commit", "-qm", "wip")
    assert (await changes(wt, sanitized_env())).commits == 1

    note = await close(wt, "remove", title="t")

    assert "Removed" in note
    assert not on_disk(wt.path)
    assert git(repo, "branch", "--list", wt.branch) == ""
    assert not (repo / "feature.txt").exists()


def test_a_stored_worktree_round_trips_and_a_cut_setup_reads_failed() -> None:
    wt = SessionWorktree(
        path="/w/repo-1",
        branch="tst/wt/1",
        project="/r/app",
        repo_top="/r",
        prefix="app",
        base="abc",
        setup="running",
    )
    back = SessionWorktree.from_record(wt.to_record())
    assert back is not None
    assert back.root == Path("/w/repo-1/app")
    assert back.setup == "failed"
    assert SessionWorktree.from_record(None) is None
    assert SessionWorktree.from_record({"path": "/x"}) is None
