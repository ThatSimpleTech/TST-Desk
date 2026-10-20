"""The static read-only command list for shell: classification (TD-5103).

A command made only of the built-in read-only entries is Class A from the
rule table, with no worker call. A redirect, a command off the list, a
write, exec or symlink-following flag, or a path outside the wall drops it
back to the shell floor (Class B), as every shell call was before. Config
can shrink the list or turn it off, never grow it.

The programs are stubs in a folder outside the workspace, so ``which``
finds them wherever the suite runs; nothing here runs a command.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import tstd.shell_read_only as shell_read_only
from tests.test_loop import make_config
from tstd.autonomy import AmbiguousClassifier, Boundary, DecisionClass, DecisionClassifier
from tstd.autonomy.classifier import RULE_TABLE, Classification
from tstd.config import ConfigError, ShellReadOnlyConfig, default_config_yaml, load_config
from tstd.loop import session_boundary
from tstd.session import Session
from tstd.shell_read_only import READ_ONLY_ENTRIES
from tstd.tools import build_decision_request, create_registry

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the list is POSIX only")

ALL = frozenset(READ_ONLY_ENTRIES)
PROGRAMS = ("ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "git", "rg", "grep", "find")


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "a.txt").write_text("hello\n")
    (root / "src" / "b.py").write_text("x = 1\n")
    (tmp_path / "outside.txt").write_text("secret\n")
    (root / "link").symlink_to(tmp_path / "outside.txt")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in PROGRAMS:
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(shell_read_only, "git_ready", lambda: True)
    return root


def classify(
    root: Path, command: str, entries: frozenset[str] = ALL, provenance: str | None = None
) -> Classification:
    tool = create_registry().get("shell")
    assert tool is not None
    request = build_decision_request(tool, {"command": command})
    if provenance is not None:
        request = replace(request, provenance=provenance)
    boundary = Boundary(workspace_root=root, shell_read_only=entries)
    return DecisionClassifier(boundary).classify(request)


def assert_floor(decision: Classification) -> None:
    assert decision.decision_class is DecisionClass.B
    assert decision.rule is not None and decision.rule.id == "shell-floor"
    assert decision.allowed_by == ()


# ── AC1: the list runs without a model verdict ──────────────────────────

LISTED = [
    ("ls", ("ls",)),
    ("ls -la src", ("ls",)),
    ("pwd", ("pwd",)),
    ("cat a.txt", ("cat",)),
    ('cat "a.txt"', ("cat",)),
    ("head -n 5 a.txt", ("head",)),
    ("tail -n20 a.txt", ("tail",)),
    ("wc -l a.txt src/b.py", ("wc",)),
    ("file a.txt", ("file",)),
    ("stat a.txt", ("stat",)),
    ("git status --short", ("git status",)),
    ("git log --oneline -5", ("git log",)),
    ("git --no-pager log -1", ("git log",)),
    ("git diff HEAD~1 -- src", ("git diff",)),
    ("git show HEAD:a.txt", ("git show",)),
    ("git show HEAD^", ("git show",)),
    ("git log HEAD~3..HEAD", ("git log",)),
    ("git branch -a", ("git branch",)),
    ("git branch -vv", ("git branch",)),
    ("git branch --list 'feat/*'", ("git branch",)),
    ("git branch --merged main", ("git branch",)),
    ("rg -n 'TODO|FIXME' src", ("rg",)),
    ("grep -rn needle .", ("grep",)),
    ("find . -name '*.py' -type f", ("find",)),
    ("git log --oneline | head -n 3", ("git log", "head")),
    ("ls src && cat a.txt; wc -l a.txt", ("ls", "cat", "wc")),
    ("cat a.txt || pwd\n", ("cat", "pwd")),
]


@pytest.mark.parametrize(("command", "entries"), LISTED)
def test_listed_commands_are_static_a(ws: Path, command: str, entries: tuple[str, ...]) -> None:
    decision = classify(ws, command)
    assert decision.decision_class is DecisionClass.A
    assert decision.rule is not None and decision.rule.id == "shell-read-only"
    assert decision.allowed_by == entries
    assert all(entry in decision.reason for entry in entries)


async def test_the_worker_is_never_asked(ws: Path) -> None:
    calls: list[str] = []

    async def worker(prompt: str) -> str:
        calls.append(prompt)
        return "A"

    tool = create_registry().get("shell")
    assert tool is not None
    for entries, expected in ((ALL, DecisionClass.A), (frozenset(), DecisionClass.B)):
        classifier = AmbiguousClassifier(
            static=DecisionClassifier(Boundary(workspace_root=ws, shell_read_only=entries)),
            call_worker=worker,
        )
        decision = await classifier.classify(
            build_decision_request(tool, {"command": "git status"})
        )
        assert decision.decision_class is expected
    assert calls == []


def test_the_grant_follows_every_c_rule_and_the_mcp_floor() -> None:
    ids = [rule.id for rule in RULE_TABLE]
    at = ids.index("shell-read-only")
    for rule in RULE_TABLE:
        if rule.decision_class is DecisionClass.C:
            assert ids.index(rule.id) < at, rule.id
    assert ids.index("mcp-undeclared-fields") < at < ids.index("side-effect-ask-floor")


def test_a_borrowed_shell_gets_no_grant(ws: Path) -> None:
    assert_floor(classify(ws, "ls", provenance="mcp:srv"))
    assert_floor(classify(ws, "ls", provenance="plugin:dist"))


def test_without_a_workspace_nothing_matches(ws: Path) -> None:
    tool = create_registry().get("shell")
    assert tool is not None
    request = build_decision_request(tool, {"command": "ls"})
    decision = DecisionClassifier(Boundary(workspace_root=None, shell_read_only=ALL)).classify(
        request
    )
    assert_floor(decision)


# ── AC2: anything else drops back to the normal gate ────────────────────

REDIRECTS = [
    "ls > out.txt",
    "ls >> out.txt",
    "git log >out.txt",
    "cat a.txt 2>/dev/null",
    "cat a.txt 2>&1",
    "cat < a.txt",
    "cat a.txt &> out.txt",
    "cat <<EOF\nx\nEOF",
    "cat <<< x",
]
OFF_LIST = [
    "ls; rm -rf src",
    "cat a.txt | sh",
    "git status && curl x",
    "ls || touch x",
    "ls & rm x",
    "ls |& cat",
    "echo hi",
    "git checkout a.txt",
    "git stash",
    "git log; git commit -m x",
    "find . | xargs rm",
    "sh -c ls",
    "env ls",
    "timeout 5 ls",
    "FOO=1 ls",
    "/bin/ls",
    "./ls",
    "command ls",
    "(ls)",
    "{ ls; }",
]
EXPANSIONS = [
    "ls $(rm x)",
    "ls `rm x`",
    "cat $HOME/x",
    'cat "$HOME"',
    'cat "a\\"b"',
    "ls *",
    "ls src/?.py",
    "cat a[1]",
    "ls {a,b}",
    "cat ~/x",
    "cat --file=~/x",
    "ls a:~/x",
    "ls ''~",
    "ls ^x",
    "ls \\x",
    "ls # note",
    "ls <(rm x)",
    "ls\r",
    "=ls",
    "ls 'a",
    "ls 'a\nb'",
]
WRITE_FLAGS = [
    "find . -delete",
    "find . -exec rm a.txt ';'",
    "find . -execdir rm a.txt ';'",
    "find . -ok rm a.txt ';'",
    "find . -fprint out.txt",
    "find . -fls out.txt",
    "find -L .",
    "find . -follow",
    "rg --pre cat x",
    "rg --pre=cat x",
    "rg --pr=cat x",
    "rg -L x",
    "rg -iL x",
    "rg -z x",
    "rg --search-zip x",
    "rg --hostname-bin=sh x",
    "grep -R x .",
    "grep -rS x .",
    "grep -rO x .",
    "grep --dereference-recursive x .",
    "ls -lL",
    "ls --dereference",
    "ls --deref",
    "wc --files0-from=list",
    "file -C",
    "file -f names",
    "file --compile",
    "git diff --output=x",
    "git log --outp=x",
    "git show --ext-diff",
    "git log --show-signature",
    "git branch newbranch",
    "git branch -v newbranch",
    "git branch -d main",
    "git branch -D main",
    "git branch -m a b",
    "git branch --set-upstream-to=origin/main",
    "git branch --edit-description",
    "git branch --sort refname",
    "git -c core.fsmonitor=x status",
    "git -C .. status",
    "git --git-dir=.git status",
    "git --work-tree=. status",
    "git --exec-path=. status",
    "git -p log",
]
OUTSIDE = [
    "cat /etc/passwd",
    "cat '/etc/passwd'",
    "cat ../outside.txt",
    "cat link",
    "ls ..",
    "ls src/../..",
    "head -n5 /etc/hosts",
    "grep -f /etc/passwd x .",
    "grep --file=/etc/passwd x",
    "rg -f/etc/passwd x",
    "find / -name x",
    "find . -newer ../outside.txt",
    "git diff --no-index ../outside.txt a.txt",
    "git log -- ../outside.txt",
]


@pytest.mark.parametrize("command", REDIRECTS + OFF_LIST + EXPANSIONS + WRITE_FLAGS + OUTSIDE)
def test_everything_else_hits_the_floor(ws: Path, command: str) -> None:
    assert_floor(classify(ws, command))


def test_a_program_inside_the_workspace_asks(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = ws / "bin"
    planted.mkdir()
    (planted / "ls").write_text("#!/bin/sh\n")
    (planted / "ls").chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted}:{tmp_path / 'bin'}")
    assert_floor(classify(ws, "ls"))
    assert classify(ws, "pwd").decision_class is DecisionClass.A


@pytest.mark.parametrize("extra", ["bin", "", "./bin"])
def test_a_relative_path_entry_asks(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{extra}")
    assert_floor(classify(ws, "ls"))


def test_a_program_not_found_asks(ws: Path, tmp_path: Path) -> None:
    (tmp_path / "bin" / "rg").unlink()
    assert_floor(classify(ws, "rg x"))


@pytest.mark.parametrize("config", [".ripgreprc", "src/rg.conf"])
def test_an_rg_config_in_the_workspace_asks(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str
) -> None:
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", config)
    assert_floor(classify(ws, "rg x"))
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(tmp_path / "rg.conf"))
    assert classify(ws, "rg x").decision_class is DecisionClass.A


def test_git_needs_the_workspace_own_repo(ws: Path) -> None:
    (ws / ".git").rmdir()
    assert_floor(classify(ws, "git status"))
    assert classify(ws, "ls").decision_class is DecisionClass.A


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GIT_DIR", "/elsewhere/.git"),
        ("GIT_WORK_TREE", "/elsewhere"),
        ("GIT_CONFIG_GLOBAL", "gitconfig"),
        ("GIT_CONFIG_SYSTEM", "src/gitconfig"),
    ],
)
def test_git_env_that_moves_the_repo_or_config_asks(
    ws: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    assert_floor(classify(ws, "git status"))
    assert classify(ws, "ls").decision_class is DecisionClass.A


def test_a_global_git_config_outside_the_workspace_is_fine(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    assert classify(ws, "git status").decision_class is DecisionClass.A


def test_git_waits_for_a_new_enough_git(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_read_only, "git_ready", lambda: False)
    assert_floor(classify(ws, "git status"))
    assert_floor(classify(ws, "ls | git log"))


def test_windows_never_matches(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_read_only, "host_dialects", lambda: ("posix", "cmd"))
    assert_floor(classify(ws, "ls"))


async def test_probe_git_reads_the_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for version, ready in (("2.39.5 (Apple Git-154)", False), ("2.40.0", True), ("3.0", True)):
        bin_dir = tmp_path / version.split()[0]
        bin_dir.mkdir()
        git = bin_dir / "git"
        git.write_text(f"#!/bin/sh\necho 'git version {version}'\n")
        git.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setattr(shell_read_only, "_git_probe", {})
        assert shell_read_only.git_ready() is False  # not probed yet: fail toward asking
        await shell_read_only.probe_git()
        assert shell_read_only.git_ready() is ready, version


# ── AC3: config holds the list, shrinks it, turns it off ────────────────


def test_the_packaged_default_is_the_whole_list() -> None:
    assert make_config().shell.read_only.entries() == ALL
    shipped = yaml.safe_load(default_config_yaml())["shell"]["read_only"]
    assert shipped["enabled"] is True
    assert tuple(shipped["commands"]) == READ_ONLY_ENTRIES


def test_config_can_shrink_the_list(ws: Path) -> None:
    entries = ShellReadOnlyConfig(commands=["ls", "git log"]).entries()
    assert entries == {"ls", "git log"}
    assert classify(ws, "ls", entries).decision_class is DecisionClass.A
    assert classify(ws, "git log", entries).decision_class is DecisionClass.A
    assert_floor(classify(ws, "cat a.txt", entries))
    assert_floor(classify(ws, "git status", entries))
    assert_floor(classify(ws, "ls | cat", entries))


def test_one_key_turns_it_off(ws: Path) -> None:
    entries = ShellReadOnlyConfig(enabled=False).entries()
    assert entries == frozenset()
    assert_floor(classify(ws, "ls", entries))


def test_config_cannot_grow_the_list(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="'rm'"):
        ShellReadOnlyConfig(commands=["ls", "rm"])
    data = yaml.safe_load(default_config_yaml())
    data["shell"] = {"read_only": {"commands": ["ls", "git push"]}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="git push"):
        load_config(path)


def test_the_session_boundary_reads_the_session_config(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    assert session_boundary(session).shell_read_only == frozenset()
    config = make_config()
    session.config = config.model_copy(
        update={
            "shell": config.shell.model_copy(
                update={"read_only": ShellReadOnlyConfig(commands=["ls"])}
            )
        }
    )
    assert session_boundary(session).shell_read_only == {"ls"}
