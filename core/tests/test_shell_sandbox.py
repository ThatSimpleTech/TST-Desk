"""Bubblewrap sandbox for interactive shell commands (TD-5138).

Acceptance criteria:

1. An opt-in sandbox (bubblewrap on Linux) runs interactive shell commands
   with writes fenced to the workspace and temp, and network off.
2. Inside the sandbox, commands that would otherwise ask can auto-run,
   configured per workspace. Commands on an exclude list run outside it,
   through the normal gate.
3. ``~/.ssh``, ``~/.aws`` and keychain paths are unreadable inside it.

The plan and gate tests run everywhere. The ones that run a real ``bwrap``
skip where the host cannot start one (no bubblewrap, or user namespaces
refused).
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tstd import shell_sandbox
from tstd.autonomy import AmbiguousClassifier, Boundary, DecisionClassifier
from tstd.autonomy.classifier import DecisionClass
from tstd.boundary_config import BoundaryConfig
from tstd.policy import ApprovalOutcome, PolicyConfig, PolicyDecision, PolicyRule
from tstd.session import Session
from tstd.shell_sandbox import SandboxSection, build_plan, excluded, promote
from tstd.tools import ToolDispatcher, create_registry, register_builtin_handlers, run_shell
from tstd.tools.boundary import PathGuard
from tstd.workspace_trust import find_loosened


def _bwrap_works() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        done = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--", "/bin/true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap cannot run here")

ON = SandboxSection(enabled=True)
AUTO = SandboxSection(enabled=True, auto_allow=True, exclude=["docker *", "git push*"])
ASK = PolicyDecision("ask", "decision class B requires approval", None)


def _session(ws: Path, section: SandboxSection) -> Session:
    session = Session(str(ws))
    session.boundary_config = BoundaryConfig(sandbox=section)
    return session


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with keys in it, so no test reads the real one."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE-SSH\n")
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text("PRIVATE-AWS\n")
    (home / ".local" / "share" / "keyrings").mkdir(parents=True)
    (home / ".local" / "share" / "keyrings" / "login.keyring").write_text("PRIVATE-KEYRING\n")
    (home / ".netrc").write_text("PRIVATE-NETRC\n")
    monkeypatch.setenv("HOME", str(home))
    return home


# ── The plan ───────────────────────────────────────────────────────────


class TestPlan:
    def test_root_read_only_workspace_writable_no_network(
        self, tmp_path: Path, fake_home: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = build_plan(str(ws), "make", ON, home=fake_home).argv
        assert argv[0] == "bwrap"
        assert "--unshare-all" in argv
        joined = " ".join(argv)
        assert "--ro-bind / /" in joined
        assert "--tmpfs /tmp" in joined
        assert f"--bind {ws.resolve()} {ws.resolve()}" in joined
        assert argv[-3:] == ["/bin/sh", "-c", "make"]

    def test_a_hidden_dir_around_the_workspace_is_masked_before_the_bind(
        self, tmp_path: Path
    ) -> None:
        """TD-5261: a worktree under the data dir, the default place for one.

        Runs everywhere, bubblewrap or not: it reads the order of the plan.
        """
        fake_home = tmp_path / "home"
        (fake_home / ".ssh").mkdir(parents=True)
        data = tmp_path / "data"
        ws = data / "worktrees" / "wt1"
        ws.mkdir(parents=True)
        (ws / "secrets").mkdir()
        section = SandboxSection(enabled=True, hidden_paths=[str(ws / "secrets")])
        argv = build_plan(str(ws), "true", section, home=fake_home, extra_hidden=[data]).argv

        def at(*run: str) -> int:
            return next(i for i in range(len(argv)) if tuple(argv[i : i + len(run)]) == run)

        around = at("--perms", "0111", "--tmpfs", str(data))
        bind = at("--bind", str(ws), str(ws))
        inside = at("--perms", "0000", "--tmpfs", str(ws / "secrets"))
        elsewhere = at("--perms", "0000", "--tmpfs", str(fake_home / ".ssh"))
        closed = at("--remount-ro", str(data))
        # Around the workspace: first, and passable. Inside it or apart: last, and closed.
        assert around < bind < closed < inside
        assert bind < elsewhere
        # Once as the passable mask and once to close it to writes (TD-5278):
        # never a second mask over the workspace.
        assert argv.count(str(data)) == 2
        closed_masks = [
            argv[i + 3]
            for i in range(len(argv) - 3)
            if argv[i : i + 3] == ["--perms", "0000", "--tmpfs"]
        ]
        assert str(data) not in closed_masks

    def test_a_symlinked_hidden_dir_around_the_workspace_is_masked_once(
        self, tmp_path: Path
    ) -> None:
        """TD-5278: the hidden list holds such a directory under both names."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        real_data = (tmp_path / "disk" / "data").resolve()
        (real_data / "worktrees" / "wt1").mkdir(parents=True)
        link = tmp_path / "data-link"
        link.symlink_to(real_data)
        # The daemon knows its data dir by the link; the workspace is under it.
        workspace = link / "worktrees" / "wt1"
        argv = build_plan(str(workspace), "true", ON, home=fake_home, extra_hidden=[link]).argv
        masks = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--tmpfs"]
        assert masks.count(str(real_data)) == 1
        assert str(link) not in masks
        assert argv.index(str(real_data)) < argv.index("--bind")

    def test_masks_come_after_the_workspace_bind(self, fake_home: Path) -> None:
        # The workspace is the home directory itself: the masks must still win.
        argv = build_plan(str(fake_home), "true", ON, home=fake_home).argv
        bind = argv.index("--bind")
        ssh = argv.index(str(fake_home / ".ssh"))
        aws = argv.index(str(fake_home / ".aws"))
        netrc = argv.index(str(fake_home / ".netrc"))
        assert bind < ssh and bind < aws and bind < netrc
        assert argv[ssh - 3 : ssh] == ["--perms", "0000", "--tmpfs"]
        assert argv[netrc - 2 : netrc] == ["--ro-bind", "/dev/null"]

    def test_steering_and_config_bound_back_read_only(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        (ws / ".tst").mkdir(parents=True)
        (ws / "AGENTS.md").write_text("rules\n")
        argv = build_plan(str(ws), "true", ON, home=tmp_path / "nohome").argv
        joined = " ".join(argv)
        for rel in ("AGENTS.md", ".tst"):
            target = ws.resolve() / rel
            assert f"--ro-bind {target} {target}" in joined

    def test_whole_git_dir_pinned_and_seccomp_on_fd_9(self, tmp_path: Path) -> None:
        # Pinning only .git/config and .git/hooks let a command rename .git away.
        ws = tmp_path / "ws"
        (ws / ".git" / "hooks").mkdir(parents=True)
        (ws / ".git" / "config").write_text("[core]\n")
        plan = build_plan(str(ws), "true", ON, home=tmp_path / "nohome")
        joined = " ".join(plan.argv)
        git = ws.resolve() / ".git"
        assert f"--ro-bind {git} {git}" in joined
        assert f"--ro-bind {git / 'config'}" not in joined
        assert plan.argv[plan.argv.index("--seccomp") + 1] == "9"
        assert plan.seccomp is not None
        assert plan.shell_text().endswith(f" 9< {plan.seccomp}")

    def test_linked_worktree_git_dir_inside_workspace_pinned(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        (ws / "inner-gitdir").mkdir(parents=True)
        (ws / ".git").write_text("gitdir: inner-gitdir\n")
        joined = " ".join(build_plan(str(ws), "true", ON, home=tmp_path / "nohome").argv)
        inner = ws.resolve() / "inner-gitdir"
        assert f"--ro-bind {inner} {inner}" in joined

    def test_seccomp_program_shape(self) -> None:
        prog = shell_sandbox.seccomp_program(0xC000003E, 41)
        assert len(prog) % 8 == 0 and len(prog) // 8 == 14
        native = shell_sandbox._seccomp_arch()
        assert native is not None
        path = shell_sandbox.seccomp_file()
        assert path.read_bytes() == shell_sandbox.seccomp_program(*native)
        assert path.stat().st_mode & 0o777 == 0o400

    def test_extra_hidden_paths(self, tmp_path: Path) -> None:
        secret = tmp_path / "vault"
        secret.mkdir()
        section = SandboxSection(enabled=True, hidden_paths=[str(secret)])
        argv = build_plan(str(tmp_path), "true", section, home=tmp_path / "nohome").argv
        assert str(secret) in argv

    def test_a_non_default_data_dir_is_hidden(self, tmp_path: Path) -> None:
        data = tmp_path / "tst-data"
        (data / "sessions").mkdir(parents=True)
        argv = build_plan(
            str(tmp_path / "ws"), "true", ON, home=tmp_path / "nohome", extra_hidden=[data]
        ).argv
        at = argv.index(str(data))
        assert argv[at - 3 : at] == ["--perms", "0000", "--tmpfs"]


# ── The gate ───────────────────────────────────────────────────────────


class TestGuard:
    """Guarded names a bind cannot pin are checked after the run."""

    def test_created_name_moved_aside(self, tmp_path: Path) -> None:
        before = shell_sandbox.guard_snapshot(str(tmp_path))
        (tmp_path / "AGENTS.md").write_text("evil\n")
        (tmp_path / ".git").mkdir()
        notes = shell_sandbox.guard_restore(str(tmp_path), before)
        assert not (tmp_path / "AGENTS.md").exists() and not (tmp_path / ".git").exists()
        assert len(notes) == 2 and all(n.startswith("blocked:") for n in notes)
        moved = sorted(p.name for p in tmp_path.iterdir())
        assert any(n.startswith("AGENTS.md.sandbox-blocked-") for n in moved)
        assert any(n.startswith(".git.sandbox-blocked-") for n in moved)

    def test_replaced_symlink_restored(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("rules\n")
        (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
        before = shell_sandbox.guard_snapshot(str(tmp_path))
        (tmp_path / "AGENTS.md").unlink()
        (tmp_path / "AGENTS.md").write_text("evil\n")
        notes = shell_sandbox.guard_restore(str(tmp_path), before)
        assert (tmp_path / "AGENTS.md").is_symlink()
        assert (tmp_path / "AGENTS.md").read_text() == "rules\n"
        assert "restored the AGENTS.md symlink" in notes[0]

    def test_unchanged_says_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("rules\n")
        before = shell_sandbox.guard_snapshot(str(tmp_path))
        assert shell_sandbox.guard_restore(str(tmp_path), before) == []


class TestPromote:
    def test_class_default_ask_becomes_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shell_sandbox, "unavailable_reason", lambda: None)
        got = promote(AUTO, "npm test", ASK, DecisionClass.B, autonomy=False)
        assert got is not None and got.effect == "auto"
        assert "sandbox" in got.reason

    @pytest.mark.parametrize(
        ("section", "command", "decision", "klass", "autonomy"),
        [
            (ON, "npm test", ASK, DecisionClass.B, False),  # auto_allow off
            (SandboxSection(auto_allow=True), "npm test", ASK, DecisionClass.B, False),  # off
            (AUTO, "docker run x", ASK, DecisionClass.B, False),  # excluded
            (AUTO, "make && git push origin", ASK, DecisionClass.B, False),  # one excluded
            (AUTO, "sudo docker ps", ASK, DecisionClass.B, False),  # excluded once peeled
            (AUTO, "npm test", ASK, DecisionClass.C, False),  # class C still asks
            (AUTO, "npm test", ASK, DecisionClass.B, True),  # autonomy has its container
            (AUTO, "echo $'x'", ASK, DecisionClass.B, False),  # unsplittable
            (
                AUTO,
                "npm test",
                PolicyDecision("ask", "rule", PolicyRule(tool="shell", effect="ask")),
                DecisionClass.B,
                False,
            ),  # the user's own ask rule
            (AUTO, "npm test", PolicyDecision("never", "no", None), DecisionClass.B, False),
            (AUTO, None, ASK, DecisionClass.B, False),
        ],
    )
    def test_stays_as_it_was(
        self,
        monkeypatch: pytest.MonkeyPatch,
        section: SandboxSection,
        command: Any,
        decision: PolicyDecision,
        klass: DecisionClass,
        autonomy: bool,
    ) -> None:
        monkeypatch.setattr(shell_sandbox, "unavailable_reason", lambda: None)
        assert promote(section, command, decision, klass, autonomy=autonomy) is None

    def test_no_bubblewrap_means_no_auto_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shell_sandbox.shutil, "which", lambda _name: None)
        assert promote(AUTO, "npm test", ASK, DecisionClass.B, autonomy=False) is None

    def test_exclude_matches_any_reading(self) -> None:
        assert excluded(AUTO, "cd sub; docker build .")
        assert not excluded(AUTO, "npm test")


class TestTrust:
    def test_repo_auto_allow_waits_for_trust(self, tmp_path: Path) -> None:
        data = {"sandbox": {"enabled": True, "auto_allow": True}}
        keys = [item.key for item in find_loosened(tmp_path / "config.yaml", data)]
        assert "sandbox.auto_allow" in keys

    def test_enabling_and_excluding_never_loosen(self, tmp_path: Path) -> None:
        data = {"sandbox": {"enabled": True, "exclude": ["docker *"]}}
        assert find_loosened(tmp_path / "config.yaml", data) == []

    def test_unknown_key_refused(self) -> None:
        with pytest.raises(ValueError):
            BoundaryConfig.model_validate({"sandbox": {"enable": True}})


# ── Real runs ──────────────────────────────────────────────────────────


@needs_bwrap
class TestRealSandbox:
    async def test_writes_fenced_to_workspace_and_temp(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        # /var/tmp is on the real root (read-only inside); /tmp is private.
        host_dir = Path(tempfile.mkdtemp(dir="/var/tmp"))
        outside = host_dir / "outside.txt"
        marker = host_dir.with_name(f"tst-sandbox-{host_dir.name}")
        marker = Path("/tmp") / marker.name
        try:
            out = await run_shell(
                _session(ws, ON),
                f"echo in > made.txt; echo t > {marker} && cat {marker}; "
                f"echo out > {outside} || echo BLOCKED",
            )
            assert shell_sandbox.SANDBOX_NOTE in out
            assert (ws / "made.txt").read_text() == "in\n"
            assert "BLOCKED" in out and not outside.exists()
            assert "t\n" in out
            assert not marker.exists()
        finally:
            shutil.rmtree(host_dir, ignore_errors=True)

    async def test_a_worktree_inside_the_data_dir_is_still_the_workspace(self) -> None:
        """TD-5261: a session's worktree lives under the data dir by default.

        The data dir is one of the hidden paths, and its mask used to land
        on top of the workspace bind, so every command failed to enter its
        own folder.
        """
        from tstd.logging import user_data_dir

        data = user_data_dir()
        ws = data / "worktrees" / "wt1"
        ws.mkdir(parents=True)
        (data / "sessions.json").write_text("DATA-DIR-SECRET", encoding="utf-8")
        (data / "worktrees" / "other").mkdir()
        (data / "worktrees" / "other" / "theirs.txt").write_text("theirs", encoding="utf-8")
        try:
            out = await run_shell(
                _session(ws, ON),
                "echo in > made.txt && cat made.txt; "
                f"cat {data}/sessions.json 2>/dev/null || echo NO-DATA; "
                f"ls {data} 2>/dev/null || echo NO-LIST; "
                f"cat {data}/worktrees/other/theirs.txt 2>/dev/null || echo NO-SIBLING",
            )
            assert shell_sandbox.SANDBOX_NOTE in out
            assert (ws / "made.txt").read_text() == "in\n"
            assert "in\n" in out
            # The rest of the data dir stays out of reach: its files, its
            # listing, and another session's worktree.
            assert "NO-DATA" in out and "DATA-DIR-SECRET" not in out
            assert "NO-LIST" in out
            assert "NO-SIBLING" in out and "theirs" not in out
        finally:
            shutil.rmtree(data / "worktrees", ignore_errors=True)
            (data / "sessions.json").unlink(missing_ok=True)

    async def test_a_worktree_under_a_symlinked_hidden_dir_still_works(self) -> None:
        """TD-5278: the mask is laid once, under the directory's real name."""
        # Not under /tmp: the sandbox gives a command a /tmp of its own, and
        # the link has to be there for the command to come in through it.
        host_dir = Path(tempfile.mkdtemp(dir="/var/tmp"))
        try:
            real_data = host_dir / "disk" / "data"
            ws = real_data / "worktrees" / "wt1"
            ws.mkdir(parents=True)
            (real_data / "secret.txt").write_text("DATA-DIR-SECRET", encoding="utf-8")
            link = host_dir / "data-link"
            link.symlink_to(real_data)
            section = SandboxSection(enabled=True, hidden_paths=[str(link)])
            out = await run_shell(
                _session(link / "worktrees" / "wt1", section),
                "echo in > made.txt && cat made.txt; "
                f"cat {link}/secret.txt 2>/dev/null || echo NO-DATA; "
                f"touch {real_data}/planted 2>/dev/null || echo NO-PLANT; "
                f"chmod 755 {real_data} 2>/dev/null || echo NO-CHMOD",
            )
            assert (ws / "made.txt").read_text() == "in\n", out
            assert "NO-DATA" in out and "DATA-DIR-SECRET" not in out
            # The mask cannot be written into or opened up from inside.
            assert "NO-PLANT" in out and "NO-CHMOD" in out
            assert not (real_data / "planted").exists()
        finally:
            shutil.rmtree(host_dir, ignore_errors=True)

    @pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
    async def test_a_worktrees_git_metadata_cannot_be_written_from_inside(
        self, tmp_path: Path
    ) -> None:
        """TD-5278: what git reads later, on the host, stays as it was.

        A writable admin dir is enough for a sandboxed command to plant a
        ``core.fsmonitor`` or redirect ``commondir``; the person's next
        ``git status`` outside the sandbox would then run it.
        """
        from tstd.logging import user_data_dir

        def git(*args: str, cwd: Path) -> str:
            done = subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
            )
            return done.stdout.strip()

        repo = tmp_path / "project"
        repo.mkdir()
        git("init", "-q", "-b", "main", cwd=repo)
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        git("add", "a.txt", cwd=repo)
        git("commit", "-q", "-m", "first", cwd=repo)
        ws = user_data_dir() / "worktrees" / "wt-git"
        ws.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "-q", "-b", "tst/wt/x", str(ws), cwd=repo)
        admin = Path(git("rev-parse", "--absolute-git-dir", cwd=ws))
        common = repo / ".git"
        assert admin.parent == common / "worktrees"

        def snapshot() -> dict[str, bytes]:
            return {
                str(path.relative_to(common)): path.read_bytes()
                for path in sorted(common.rglob("*"))
                if path.is_file()
            }

        before = snapshot()
        try:
            out = await run_shell(
                _session(ws, ON),
                "git status --short >/dev/null 2>&1 && echo STATUS-OK || echo STATUS-FAILED; "
                "echo two > a.txt; "
                f"echo '[core]' >> {admin}/config.worktree 2>/dev/null || echo NO-WT-CONFIG; "
                f"echo /tmp/elsewhere > {admin}/commondir 2>/dev/null || echo NO-COMMONDIR; "
                f"echo x >> {common}/config 2>/dev/null || echo NO-CONFIG; "
                f"echo x > {common}/hooks/pre-commit 2>/dev/null || echo NO-HOOK; "
                "git add a.txt 2>/dev/null && echo ADDED || echo NO-ADD",
            )
            # The work itself lands in the worktree.
            assert (ws / "a.txt").read_text() == "two\n"
            for refused in ("NO-WT-CONFIG", "NO-COMMONDIR", "NO-CONFIG", "NO-HOOK", "NO-ADD"):
                assert refused in out, out
            assert snapshot() == before, "git metadata changed from inside the sandbox"
            # And the host's git still sees an ordinary, uncommitted change.
            assert git("status", "--short", cwd=ws).strip() == "M a.txt"
        finally:
            shutil.rmtree(user_data_dir() / "worktrees", ignore_errors=True)

    async def test_timeout_kills_the_sandboxed_tree(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        # A survivor would write late.txt into the (bound) workspace.
        out = await run_shell(
            _session(ws, ON), "{ sleep 2; touch late.txt; } & sleep 30; wait", timeout_secs=1
        )
        assert "timed out" in out
        await asyncio.sleep(3)
        assert not (ws / "late.txt").exists(), "a sandboxed child outlived the kill"

    async def test_steering_file_read_only(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "AGENTS.md").write_text("rules\n")
        out = await run_shell(_session(ws, ON), "echo evil >> AGENTS.md || echo BLOCKED")
        assert "BLOCKED" in out
        assert (ws / "AGENTS.md").read_text() == "rules\n"

    async def test_network_off(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            probe = (
                f"{sys.executable} -c \"import socket; socket.create_connection(('127.0.0.1', "
                f"{port}), timeout=3); print('REACHED')\" || echo NO-NET"
            )
            out = await run_shell(_session(ws, ON), probe)
        finally:
            server.close()
        assert "NO-NET" in out and "REACHED" not in out

    async def test_no_host_unix_sockets(self, tmp_path: Path) -> None:
        # A read-only mount does not stop connect() on a socket file
        # (docker.sock, tailscaled); the seccomp filter does. The listener
        # sits in the workspace, which the sandbox can see.
        ws = tmp_path / "ws"
        ws.mkdir()
        path = ws / "h.sock"
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen(1)
        try:
            probe = (
                f'{sys.executable} -c "import socket; s = socket.socket(socket.AF_UNIX); '
                f"s.settimeout(3); s.connect('{path}'); print('REACHED')\" || echo NO-SOCK; "
                f'{sys.executable} -c "import socket; a, b = socket.socketpair(); '
                f"a.send(b'ok'); print('PAIR', b.recv(2))\"; "
                f'{sys.executable} -c "import ctypes; libc = ctypes.CDLL(None, use_errno=True); '
                f"r = libc.syscall(425, 1, ctypes.create_string_buffer(120)); "
                f"print('URING', r, ctypes.get_errno())\""
            )
            out = await run_shell(_session(ws, ON), probe)
        finally:
            server.close()
        assert "NO-SOCK" in out and "REACHED" not in out
        assert "PAIR b'ok'" in out
        assert "URING -1 1" in out

    @pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
    async def test_git_dir_cannot_be_swapped_for_a_planted_config(self, tmp_path: Path) -> None:
        # The review's escape: rename .git, write a fresh one whose
        # core.fsmonitor the host's next git status runs.
        ws = tmp_path / "ws"
        ws.mkdir()
        await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(ws)], check=True)
        pwned = tmp_path / "PWNED"
        attack = (
            "mv .git .git-old && mkdir .git && cp -r .git-old/objects .git-old/refs "
            ".git-old/HEAD .git/ && printf '[core]\\n\\trepositoryformatversion = 0\\n"
            f"\\tfsmonitor = touch {pwned}\\n' > .git/config && echo SWAPPED; "
            f"printf '[core]\\n\\tfsmonitor = touch {pwned}\\n' >> .git/config "
            "&& echo PLANTED; echo x > .git/hooks/pre-commit && echo HOOKED; true"
        )
        out = await run_shell(_session(ws, ON), attack)
        assert "SWAPPED" not in out and "PLANTED" not in out and "HOOKED" not in out
        await asyncio.to_thread(
            subprocess.run, ["git", "status"], cwd=ws, capture_output=True, check=False
        )
        assert not pwned.exists(), "the host git ran a planted fsmonitor"

    async def test_missing_steering_file_created_inside_is_moved_aside(
        self, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        out = await run_shell(_session(ws, ON), "echo evil > AGENTS.md; mkdir .git")
        assert not (ws / "AGENTS.md").exists() and not (ws / ".git").exists()
        assert "blocked: the sandboxed command changed AGENTS.md" in out
        assert "blocked: the sandboxed command changed .git" in out

    async def test_keys_unreadable(self, tmp_path: Path, fake_home: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        out = await run_shell(
            _session(ws, ON),
            "cat ~/.ssh/id_ed25519 ~/.aws/credentials ~/.netrc "
            "~/.local/share/keyrings/login.keyring; ls ~/.ssh || echo SSH-HIDDEN",
        )
        assert "PRIVATE" not in out
        assert "SSH-HIDDEN" in out

    async def test_background_job_is_sandboxed_and_guarded(self, tmp_path: Path) -> None:
        # TD-5137 merge: background: true is the same shell call, so it
        # must not be a way around the sandbox.
        ws = tmp_path / "ws"
        ws.mkdir()
        # /var/tmp is on the real root (read-only inside); /tmp is private.
        host_dir = Path(tempfile.mkdtemp(dir="/var/tmp"))
        outside = host_dir / "outside.txt"
        session = _session(ws, ON)
        try:
            started = await run_shell(
                session,
                f"echo out > {outside} || echo BLOCKED; echo evil > AGENTS.md",
                background=True,
            )
            assert shell_sandbox.SANDBOX_NOTE in started
            job = session.shell_jobs.get("job-1")
            assert job is not None
            for _ in range(200):
                if job.status != "running":
                    break
                await asyncio.sleep(0.05)
            assert job.status == "exited"
            assert not outside.exists()
            assert not (ws / "AGENTS.md").exists()
            text, _ = job.output.read()
            assert "BLOCKED" in text
            assert "blocked: the sandboxed command changed AGENTS.md" in text
        finally:
            await session.shell_jobs.stop_all()
            shutil.rmtree(host_dir, ignore_errors=True)

    async def test_timed_out_command_moved_to_a_job_stays_guarded(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        session = _session(ws, ON)
        try:
            out = await run_shell(
                session,
                "sleep 1.5; echo evil > CLAUDE.md",
                timeout_secs=1,
                on_timeout="background",
            )
            assert "moved to background job job-1" in out
            assert shell_sandbox.SANDBOX_NOTE in out
            job = session.shell_jobs.get("job-1")
            assert job is not None
            for _ in range(200):
                if job.status != "running":
                    break
                await asyncio.sleep(0.05)
            assert job.status == "exited"
            assert not (ws / "CLAUDE.md").exists()
            text, _ = job.output.read()
            assert "blocked: the sandboxed command changed CLAUDE.md" in text
        finally:
            await session.shell_jobs.stop_all()

    async def test_excluded_command_runs_on_the_host(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside.txt"
        section = SandboxSection(enabled=True, exclude=["echo host*"])
        out = await run_shell(_session(ws, section), f"echo host > {outside}")
        assert shell_sandbox.SANDBOX_NOTE not in out
        assert outside.read_text() == "host\n"


class TestRefusals:
    async def test_promised_call_refused_when_sandbox_turned_off(self, tmp_path: Path) -> None:
        session = _session(tmp_path, SandboxSection())
        session.bwrap_promised.add("call-1")
        with pytest.raises(ValueError, match="no longer applies"):
            await run_shell(session, "touch made.txt", tool_call_id="call-1")
        assert not (tmp_path / "made.txt").exists()

    async def test_no_bubblewrap_refuses_instead_of_running_on_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shell_sandbox.shutil, "which", lambda _name: None)
        with pytest.raises(ValueError, match="bubblewrap"):
            await run_shell(_session(tmp_path, ON), "touch made.txt")
        assert not (tmp_path / "made.txt").exists()


# ── Through the dispatcher ─────────────────────────────────────────────


async def _stub_worker(_prompt: str) -> str:
    return "B"


def _dispatcher(ws: Path, asked: list[str]) -> ToolDispatcher:
    boundary = Boundary(workspace_root=ws)
    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(boundary), call_worker=_stub_worker
        ),
        path_guard=PathGuard(boundary),
    )
    dispatcher.policy = PolicyConfig()

    async def _approve(_id: str, _tool: Any, arguments: dict[str, Any], *_rest: Any) -> Any:
        asked.append(str(arguments.get("command")))
        return ApprovalOutcome(True)

    dispatcher.approval_handler = _approve
    register_builtin_handlers(dispatcher)
    return dispatcher


@needs_bwrap
class TestGateEndToEnd:
    async def test_auto_allow_runs_without_asking(self, tmp_path: Path) -> None:
        asked: list[str] = []
        session = _session(tmp_path, AUTO)
        result = await _dispatcher(tmp_path, asked).dispatch(
            "c1", "shell", {"command": "touch made.txt"}, session
        )
        assert result.status == "success", result.output
        assert asked == []
        assert (tmp_path / "made.txt").exists()
        assert shell_sandbox.SANDBOX_NOTE in result.output

    async def test_excluded_command_asks_and_runs_outside(self, tmp_path: Path) -> None:
        asked: list[str] = []
        session = _session(
            tmp_path, SandboxSection(enabled=True, auto_allow=True, exclude=["touch *"])
        )
        result = await _dispatcher(tmp_path, asked).dispatch(
            "c2", "shell", {"command": "touch made.txt"}, session
        )
        assert asked == ["touch made.txt"]
        assert shell_sandbox.SANDBOX_NOTE not in result.output

    async def test_without_auto_allow_it_still_asks(self, tmp_path: Path) -> None:
        asked: list[str] = []
        result = await _dispatcher(tmp_path, asked).dispatch(
            "c3", "shell", {"command": "touch made.txt"}, _session(tmp_path, ON)
        )
        assert asked == ["touch made.txt"]
        assert shell_sandbox.SANDBOX_NOTE in result.output
