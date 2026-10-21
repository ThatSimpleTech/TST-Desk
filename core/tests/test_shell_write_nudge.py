"""Shell file writes are nudged toward fs_edit / fs_write (TD-5009).

A shell command that writes a workspace file skips the diff, the
checkpoint and review.  The base prompt and the tool descriptions say so,
and a shell result whose command looks like a workspace write carries a
note.  The note never changes how or whether the command runs.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable
from pathlib import Path

import pytest

from tstd.context.prompt import BASE_SYSTEM_PROMPT, PromptAssembler
from tstd.session import Session
from tstd.tools import shell as shell_module
from tstd.tools.registry import create_registry
from tstd.tools.shell import run_shell
from tstd.tools.shell_writes import SHELL_WRITE_NOTE, writes_workspace_files

# `{ws}` is replaced with the workspace path.  The workspace sits under
# pytest's tmp_path, which is itself under /tmp, so the /tmp cases also
# prove that "outside the workspace" is judged by containment.
WRITES: list[str] = [
    "echo hi > notes.txt",
    "echo hi >> notes.txt",
    "printf 'x' >notes.txt",
    "ls &> listing.txt",
    "make 2>&1 > build.txt",
    "echo x > {ws}/inside.txt",
    'echo x > "$OUT"',
    "ls\necho done > status.txt",
    "cd src && echo x > local.txt",
    "cat <<EOF > src/app.py\nprint('a > b')\nEOF",
    "cat > src/app.py <<'EOF'\nimport os\nEOF",
    "sed -i 's/foo/bar/' src/app.py",
    "sed -i.bak -e 's/a/b/' src/app.py",
    "sed --in-place 's/a/b/' README.md",
    "sed -Ei 's/a+/b/' src/app.py",
    "sed -i '' 's/a/b/' src/app.py",
    "perl -pi -e 's/foo/bar/' src/app.py",
    "perl -i.bak -pe 's/a/b/' src/app.py",
    "perl -0777 -pi -e 's/a/b/s' src/app.py",
    "grep -rl foo src | xargs sed -i 's/foo/bar/'",
    "find . -name '*.py' -exec sed -i 's/a/b/' {} +",
    "gawk -i inplace '{print}' data.txt",
    "pytest -q 2>&1 | tee test-output.txt",
    "echo x | sudo tee -a config.ini",
    "dd if=/dev/zero of=blob.bin bs=1k count=1",
    "python3 -c \"open('out.txt', 'w').write('x')\"",
    "python -c \"from pathlib import Path; Path('a.txt').write_text('x')\"",
    "python3 -c \"import sys; open(sys.argv[1], mode='a').write('x')\" f.txt",
    "python3 -c \"p = 'notes.md'; open(p, 'a').write('x')\"",
    "python3 - <<'EOF'\nwith open('src/app.py', 'a') as f:\n    f.write('x')\nEOF",
    "node -e \"require('fs').writeFileSync('out.json', '{}')\"",
    # Input redirects are read; the output target still counts.
    "tee out.txt < in.txt",
    "sudo -u www-data tee config.ini < new.ini",
    # A wrapper's option value is not the program.
    "env -u PYTHONPATH sed -i 's/a/b/' setup.cfg",
    "timeout -s KILL 30 sed -i 's/a/b/' f",
    "env -u PYTHONHOME -u PYTHONPATH python3 -c \"open('a.txt', 'w')\"",
    "nice -n 10 sed -i 's/a/b/' f",
    "xargs -n 1 sed -i 's/a/b/' < files.txt",
    # The cwd leaves a subshell, `popd` and a failed `cd` lose it.
    "(cd /tmp && make); echo x > notes.md",
    "( cd /tmp; make ); echo x > notes.md",
    "pushd /tmp && make; popd; echo x > notes.md",
    "cd /nonexistent || echo x > out.txt",
    "cd \"$X\" && python3 - <<'PY'\nopen('x.txt', 'w')\nPY",
    # `<<` inside quotes opens no heredoc.
    "grep -n '<<EOF' scripts/build.sh\necho done > status.txt",
    # A heredoc piped into python is code.
    "cat <<'EOF' | python3 -\nopen('out.txt', 'w').write('x')\nEOF",
    # A shell's `-c` script and stdin are read like the command itself.
    "bash -c 'echo x > out.txt'",
    "sh -c \"sed -i 's/a/b/' f\"",
    "env FOO=1 bash -lc 'echo x > out.txt'",
    "bash <<'EOF'\necho x > out.txt\nEOF",
    "python3 -Bc \"open('a.txt', 'w')\"",
    # An arithmetic shift opens no heredoc that would hide the next line.
    "echo $((1 << 2))\necho x > out.txt",
]

READ_ONLY: list[str] = [
    "ls -la",
    'grep -rn "foo" src',
    "rg -n 'a > b' src",
    'grep ">" index.html',
    "git log --oneline -5",
    "git diff",
    "git status --short",
    "pytest -q 2>&1 | tail -5",
    "find . -name '*.py'",
    "cat README.md",
    "echo hi",
    "[[ a > b ]] && echo yes",
    "(( count > 3 )) && echo many",
    "echo $((1 > 0))",
    "make > /dev/null 2>&1",
    "cmd 2>/dev/null",
    "echo x >&2",
    "ls 2>&1 >/dev/null",
    "pytest > /tmp/pytest.log",
    'echo x > "$TMPDIR/scratch.txt"',
    "echo x > ~/notes.txt",
    "cd /tmp && echo x > scratch.txt",
    "echo x > ../outside.txt",
    "sed 's/a/b/' file.txt",
    "sed -n '1,20p' src/app.py",
    "sed -e 's/i/j/' file.txt",
    "perl -ne 'print if /x/' file.txt",
    "perl -Ilib -e 'print 1'",
    "sed -i 's/a/b/' /tmp/elsewhere.txt",
    "cat <<EOF\nif a > b then\nEOF",
    "git commit -m \"$(cat <<'EOF'\nfix: a > b\nEOF\n)\"",
    "python3 -c \"print(open('README.md').read())\"",
    'python3 -c "x = 3 > 2; print(x)"',
    "python3 -c \"open('/tmp/x.txt', 'w').write('1')\"",
    "python3 - <<'EOF'\np = '/srv/models/MANIFEST.md'\nopen(p, 'w').write('x')\nEOF",
    'echo ".worktrees/" >> .git/info/exclude',
    "python3 script.py > /dev/null",
    'node -e "console.log(1 > 0)"',
    "echo x | tee /dev/stderr",
    "echo x | tee",
    "ls # list > files",
    "awk '$1 > 5' data.txt",
    "diff <(sort a) <(sort b)",
    "dd if=disk.img of=/dev/null",
    "echo 'unterminated > x",
    # Input redirects, here-strings and process substitution are read.
    "tee < in.txt",
    "sed -n p < in.txt",
    "wc -l < data.csv",
    "cat <<< 'a > b'",
    'while read l; do echo "$l"; done < list.txt',
    "python3 script.py < input.txt",
    # A heredoc fed to a script is data, not code.
    "python3 tool.py <<'EOF'\nopen('x.txt', 'w')\nEOF",
    # The cwd at the heredoc is outside the workspace.
    "cd /tmp/probe && python3 - <<'PY'\nopen('x.txt', 'w').write('a')\nPY",
    "( cd /tmp && echo x > a.txt )",
    # Null devices, temp variables, mktemp names, os.devnull.
    "cmd > nul",
    "cmd > NUL 2>&1",
    "Get-ChildItem > $null",
    'ls > "${TMPDIR:-/tmp}/list.txt"',
    'ls > "${TMP}/list.txt"',
    'out=$(mktemp); ls > "$out"',
    'dir=$(mktemp -d) && echo x > "$dir/a.txt"',
    "python3 -c \"import os; open(os.devnull, 'w')\"",
    # Captured logs are output, not file changes.
    "pytest -q > test.log 2>&1",
    "make 2>&1 | tee build.log",
    "nohup ./server > server.log 2>&1 &",
    "bash script.sh",
]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    return ws


class TestDetection:
    @pytest.mark.parametrize("command", WRITES)
    def test_workspace_write_is_flagged(self, command: str, workspace: Path) -> None:
        assert writes_workspace_files(command.replace("{ws}", str(workspace)), workspace)

    @pytest.mark.parametrize("command", READ_ONLY)
    def test_read_or_scratch_command_is_not_flagged(self, command: str, workspace: Path) -> None:
        assert not writes_workspace_files(command, workspace)

    def test_absolute_path_outside_the_workspace_is_not_flagged(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        sibling = tmp_path / "other" / "file.txt"
        assert not writes_workspace_files(f"echo x > {sibling}", workspace)
        assert not writes_workspace_files(f"sed -i 's/a/b/' {sibling}", workspace)

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_symlinked_workspace_matches_its_real_path(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert writes_workspace_files(f"echo x > {real}/a.txt", link)

    def test_container_mount_counts_as_the_workspace(self, workspace: Path) -> None:
        command = "echo x > /workspace/out.txt"
        assert not writes_workspace_files(command, workspace)
        assert writes_workspace_files(command, workspace, mount="/workspace")
        assert writes_workspace_files(
            "cd /workspace/src && sed -i 's/a/b/' a.py", workspace, mount="/workspace"
        )
        assert not writes_workspace_files("echo x > /tmp/out.txt", workspace, mount="/workspace")

    @pytest.mark.parametrize(
        "command",
        [
            "python3 - <<'EOF'\n" + "x = open(\n" * 5000 + "EOF",
            "python3 - <<'EOF'\n" + "open(a, " * 20000 + "\nEOF",
            'python -c "' + "open('a'" * 20000 + '"',
            "node - <<'EOF'\n" + "writeFileSync(`" * 20000 + "\nEOF",
        ],
        ids=["unclosed-lines", "unclosed-heredoc", "unclosed-inline", "unclosed-node"],
    )
    def test_pathological_inline_code_is_bounded(self, command: str, workspace: Path) -> None:
        # Reviewers measured 12-290 s here before the scan was bounded; the
        # check runs on the daemon's event loop after every shell call.
        started = time.perf_counter()
        assert writes_workspace_files(command, workspace)
        assert time.perf_counter() - started < 2.0

    def test_realistic_reads_stay_unflagged_under_the_bound(self, workspace: Path) -> None:
        body = "with open('data/f.txt') as fh:\n    rows = fh.read()\n" * 400
        assert not writes_workspace_files(f"python3 - <<'EOF'\n{body}EOF", workspace)


class TestShellResult:
    async def test_write_gets_the_note_and_still_runs(self, workspace: Path) -> None:
        result = await run_shell(Session(str(workspace)), "echo hi > out.txt")
        assert (workspace / "out.txt").read_text(encoding="utf-8").strip() == "hi"
        assert SHELL_WRITE_NOTE in result
        # Under the exit code, ahead of the streams: a capped result keeps it.
        assert result.index("exit code: 0") < result.index(SHELL_WRITE_NOTE)
        assert result.index(SHELL_WRITE_NOTE) < result.index("── stdout ──")

    async def test_read_only_command_has_no_note(self, workspace: Path) -> None:
        result = await run_shell(Session(str(workspace)), "echo hi")
        assert "exit code: 0" in result
        assert SHELL_WRITE_NOTE not in result

    def test_note_is_forward_looking(self) -> None:
        # Text is all the detector reads: the write may have failed, and a
        # literal model must not resend a file it already wrote.
        assert "looks like" in SHELL_WRITE_NOTE
        assert "no need to redo" in SHELL_WRITE_NOTE
        assert "wrote files" not in SHELL_WRITE_NOTE

    async def test_failed_write_note_claims_no_write(self, workspace: Path) -> None:
        result = await run_shell(Session(str(workspace)), "sed -i 's/a/b/' does_not_exist.txt")
        assert "exit code: 0" not in result
        assert SHELL_WRITE_NOTE in result

    async def test_autonomous_command_judges_the_container_mount(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The container is not started here: the note is read from the text.
        def fake_open(
            session: Session, command: str, cap: object = None, sandboxed: bool = False
        ) -> Awaitable[asyncio.subprocess.Process]:
            return asyncio.create_subprocess_shell(
                "exit 0",
                cwd=session.workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        monkeypatch.setattr(shell_module, "_open_shell_process", fake_open)
        session = Session(str(workspace))
        session.autonomy = True
        assert SHELL_WRITE_NOTE in await run_shell(session, "echo x > /workspace/out.txt")
        session.autonomy = False
        assert SHELL_WRITE_NOTE not in await run_shell(session, "echo x > /workspace/out.txt")


class TestStandingInstructions:
    def test_base_prompt_names_the_file_tools_and_the_shell_forms(self) -> None:
        assert "fs_edit" in BASE_SYSTEM_PROMPT
        assert "fs_write" in BASE_SYSTEM_PROMPT
        for form in ("sed -i", "redirects", "heredocs", "tee", "inline scripts"):
            assert form in BASE_SYSTEM_PROMPT

    def test_base_prompt_keeps_the_shell_for_tool_moves(self) -> None:
        # A rule that forbade `rm`, `git mv` or `ruff format` could not be followed.
        for allowed in (
            "deleting",
            "renaming",
            "formatters",
            "code generators",
            "package managers",
        ):
            assert allowed in BASE_SYSTEM_PROMPT

    @pytest.mark.parametrize("tier", ["brain", "worker", "validator"])
    def test_every_tier_gets_the_rule_in_a_stable_prefix(
        self, tier: str, tmp_path: Path, workspace: Path
    ) -> None:
        assembler = PromptAssembler(workspace, home_dir=tmp_path / "home")
        first = assembler.assemble_sync(tier, task="edit a file")  # type: ignore[arg-type]
        second = assembler.assemble_sync(tier, task="another task")  # type: ignore[arg-type]
        assert first.prefix.startswith(BASE_SYSTEM_PROMPT)
        assert first.prefix_hash == second.prefix_hash

    def test_tool_descriptions_say_when_to_use_each(self) -> None:
        registry = create_registry()
        descriptions = {
            name: tool.description
            for name in ("fs_edit", "fs_write", "shell")
            if (tool := registry.get(name)) is not None
        }
        assert "existing file" in descriptions["fs_edit"]
        assert "whole file" in descriptions["fs_write"]
        assert "fs_edit" in descriptions["shell"] and "fs_write" in descriptions["shell"]
        assert "package managers" in descriptions["shell"]
