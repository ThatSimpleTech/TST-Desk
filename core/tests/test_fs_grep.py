"""Tests for content search and path globs (TD-5102).

Covers the acceptance criteria: ``fs_grep`` with regex, path, glob, case
flag, context lines and the files / content / count modes; capped results
with a truncation note; ``.gitignore`` honoured; Class A inside the wall
and refused outside it; ``fs_list`` path globs, modified-time sort and a
stated truncation; and the base-prompt nudge toward the search tools.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_read_tools import make_dispatcher
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier
from tstd.context.prompt import BASE_SYSTEM_PROMPT
from tstd.tools import build_decision_request, create_registry, fs_grep, fs_list, search
from tstd.tools.delegate import WORKER_CHILD_TOOLS
from tstd.tools.search import MAX_FILE_BYTES, glob_matcher, grep


def _tree(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body)


# ── glob dialect ─────────────────────────────────────────────────────────


class TestGlobMatcher:
    def test_name_glob_matches_at_any_depth(self) -> None:
        m = glob_matcher("*.py")
        assert m("a.py") and m("pkg/sub/b.py")
        assert not m("a.pyc") and not m("py/readme.md")

    def test_double_star_matches_zero_or_more_folders(self) -> None:
        m = glob_matcher("src/**/*.ts")
        assert m("src/a.ts") and m("src/x/y/z.ts")
        assert not m("lib/a.ts") and not m("src/a.tsx")

    def test_single_star_stays_in_one_folder(self) -> None:
        m = glob_matcher("src/*.ts")
        assert m("src/a.ts")
        assert not m("src/x/a.ts")

    def test_braces_question_and_negated_class(self) -> None:
        assert glob_matcher("*.{ts,svelte}")("ui/App.svelte")
        assert glob_matcher("v?.txt")("v1.txt") and not glob_matcher("v?.txt")("v10.txt")
        assert glob_matcher("[!a]*.md")("b.md") and not glob_matcher("[!a]*.md")("a.md")

    def test_leading_dot_slash_is_dropped(self) -> None:
        assert glob_matcher("./docs/*.md")("docs/x.md")


# ── grep: modes, flags, caps ─────────────────────────────────────────────


class TestGrep:
    def test_files_mode_lists_matching_files(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"a.py": "def foo():\n", "b.py": "x = 1\n", "c/d.py": "foo()\n"})
        assert grep(tmp_path, r"foo") == "a.py\nc/d.py"

    def test_content_mode_numbers_lines(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"a.py": "one\nfoo two\nthree\nfoo four\n"})
        out = grep(tmp_path, "foo", output_mode="content")
        assert out == "a.py:2: foo two\na.py:4: foo four"

    def test_context_lines_marked_and_separated(self, tmp_path: Path) -> None:
        body = "\n".join(f"l{i}" for i in range(1, 11)) + "\n"
        body = body.replace("l2\n", "hit\n").replace("l9\n", "hit\n")
        _tree(tmp_path, {"a.txt": body})
        out = grep(tmp_path, "hit", output_mode="content", context=1)
        assert out.splitlines() == [
            "a.txt-1- l1",
            "a.txt:2: hit",
            "a.txt-3- l3",
            "--",
            "a.txt-8- l8",
            "a.txt:9: hit",
            "a.txt-10- l10",
        ]

    def test_count_mode(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"a.py": "x\nx\n", "b.py": "x\n", "c.py": "y\n"})
        assert grep(tmp_path, "x", output_mode="count") == "a.py:2\nb.py:1"

    def test_case_flag(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"a.txt": "Hello\n"})
        assert grep(tmp_path, "hello") == "(no matches)"
        assert grep(tmp_path, "hello", case_insensitive=True) == "a.txt"

    def test_glob_filter(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"src/a.ts": "foo\n", "src/b.py": "foo\n", "lib/c.ts": "foo\n"})
        assert grep(tmp_path, "foo", glob="src/**/*.ts") == "src/a.ts"
        assert grep(tmp_path, "foo", glob="*.ts") == "lib/c.ts\nsrc/a.ts"

    def test_single_file_path(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"a.txt": "x\nfoo\n", "b.txt": "foo\n"})
        assert grep(tmp_path / "a.txt", "foo", output_mode="content") == "a.txt:2: foo"

    def test_results_capped_with_truncation_note(self, tmp_path: Path) -> None:
        _tree(tmp_path, {f"f{i:02}.txt": "hit\n" for i in range(12)})
        out = grep(tmp_path, "hit", max_results=5)
        lines = out.splitlines()
        assert lines[:5] == [f"f{i:02}.txt" for i in range(5)]
        assert "truncated: showing the first 5 of 12 files" in lines[-1]

    def test_content_cap_counts_matching_lines(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"a.txt": "hit\n" * 7})
        out = grep(tmp_path, "hit", output_mode="content", max_results=3)
        assert out.count("a.txt:") == 3
        assert "showing the first 3 of 7 matching lines" in out

    def test_no_matches_and_bad_regex_and_bad_mode(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"a.txt": "x\n"})
        assert grep(tmp_path, "zzz") == "(no matches)"
        assert grep(tmp_path, "(").startswith("Error: invalid regex")
        assert grep(tmp_path, "x", output_mode="lines").startswith("Error: output_mode")
        assert grep(tmp_path / "missing", "x").startswith("Error: path not found")

    def test_binary_and_oversized_files_skipped(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"bin.dat": b"hit\0\x01\x02", "ok.txt": "hit\n"})
        (tmp_path / "big.log").write_text("hit\n" + "x" * MAX_FILE_BYTES)
        assert grep(tmp_path, "hit") == "ok.txt"

    def test_long_lines_clipped(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"min.js": "hit" + "x" * 1000 + "\n"})
        out = grep(tmp_path, "hit", output_mode="content")
        assert out.endswith(" …") and len(out) < 400

    def test_walk_skips_ignored_folders_outside_git(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"node_modules/x/a.js": "hit\n", ".venv/b.py": "hit\n", "c.py": "hit\n"})
        assert grep(tmp_path, "hit") == "c.py"

    @pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
    def test_gitignore_honoured(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        _tree(
            tmp_path,
            {
                ".gitignore": "build/\n*.log\n",
                "build/out.js": "hit\n",
                "x.log": "hit\n",
                "src/a.py": "hit\n",
            },
        )
        # Untracked but not ignored files are searched too.
        assert grep(tmp_path, "hit") == "src/a.py"

    @pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
    @pytest.mark.skipif(os.name == "nt", reason="the hook below is a POSIX shell command")
    def test_repo_config_cannot_run_a_command(self, tmp_path: Path) -> None:
        """fs_grep auto-runs (TD-5141), so git must not run core.fsmonitor
        from a planted nested .git or a committed bare-repo folder."""
        marker = tmp_path / "ran"
        hook = f'fsmonitor = "touch {marker}; false"'
        ws = tmp_path / "ws"
        subprocess.run(["git", "init", "-q", str(ws)], check=True)
        _tree(ws, {"src/a.py": "hit\n", "sub/b.py": "hit\n", "vendor/lib/c.py": "hit\n"})
        nested = ws / "sub" / ".git"
        for d in ("objects", "refs"):
            (nested / d).mkdir(parents=True)
        (nested / "HEAD").write_text("ref: refs/heads/main\n")
        (nested / "config").write_text(f"[core]\n\trepositoryformatversion = 0\n\t{hook}\n")
        bare = ws / "vendor" / "lib"
        for d in ("objects", "refs"):
            (bare / d).mkdir()
        (bare / "HEAD").write_text("ref: refs/heads/main\n")
        (bare / "config").write_text(
            f"[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tworktree = .\n\t{hook}\n"
        )
        assert grep(ws / "sub", "hit") == "b.py"
        assert "c.py" in grep(ws / "vendor" / "lib", "hit")
        assert not marker.exists()

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_link_out_of_the_root_is_not_followed(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (tmp_path / "secret.txt").write_text("hit\n")
        (ws / "in.txt").write_text("hit\n")
        (ws / "link.txt").symlink_to(tmp_path / "secret.txt")
        out = grep(ws, "hit")
        assert out.splitlines()[0] == "in.txt"
        assert "link.txt" not in out.splitlines()
        assert "skipped 1 linked file" in out

    async def test_async_handler_passes_arguments(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"a.py": "Foo\n"})
        out = await fs_grep(None, "foo", str(tmp_path), case_insensitive=True, output_mode="count")
        assert out == "a.py:1"


# ── a pattern that backtracks cannot hang the turn or the daemon ─────────

# Exponential for a backtracking engine: every split of the a's into
# "a" and "aa" is tried before the "!" fails the match.
_BACKTRACKS = r"(a|aa)+$"
_BAD_LINE = "a" * 60 + "!"


class TestSlowPatterns:
    def test_a_backtracking_pattern_stops_with_what_it_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(search, "LINE_MATCH_SECONDS", 0.2)
        _tree(tmp_path, {"a.txt": "aaaa\n", "b.txt": f"{_BAD_LINE}\n", "c.txt": "aa\n"})
        started = time.monotonic()
        out = grep(tmp_path, _BACKTRACKS, output_mode="content")
        assert time.monotonic() - started < 5.0
        assert "a.txt:1: aaaa" in out
        assert "c.txt" not in out  # the search stopped at b.txt
        assert "took over 0.2 s on b.txt:1" in out
        assert "simplify" in out

    def test_a_search_has_an_overall_time_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(search, "MAX_SEARCH_SECONDS", 0.0)
        _tree(tmp_path, {"a.txt": "hit\n"})
        out = grep(tmp_path, "hit")
        assert "stopped after 0 s" in out

    async def test_the_event_loop_runs_while_a_line_is_matched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(search, "LINE_MATCH_SECONDS", 1.0)
        _tree(tmp_path, {"b.txt": f"{_BAD_LINE}\n"})
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticker = asyncio.ensure_future(tick())
        try:
            out = await fs_grep(None, _BACKTRACKS, str(tmp_path))
        finally:
            ticker.cancel()
        assert "took over 1 s" in out
        # The match runs outside the GIL; the loop kept ticking meanwhile.
        assert ticks >= 20

    async def test_stopping_the_turn_ends_the_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(search, "LINE_MATCH_SECONDS", 1.5)
        _tree(tmp_path, {f"b{n}.txt": f"{_BAD_LINE}\n" for n in range(5)})

        class _Session:
            def __init__(self) -> None:
                self.stop = asyncio.Event()

            async def wait_for_turn_abort(self) -> None:
                await self.stop.wait()

        session = _Session()
        asyncio.get_running_loop().call_later(0.2, session.stop.set)
        started = time.monotonic()
        out = await fs_grep(session, _BACKTRACKS, str(tmp_path))
        assert time.monotonic() - started < 1.0
        assert out.startswith("Search stopped")


# ── registry, classification, dispatch ───────────────────────────────────


class TestFsGrepGate:
    def test_registered_as_parallel_safe_read_tool(self) -> None:
        tool = create_registry().get("fs_grep")
        assert tool is not None
        assert tool.side_effect_class == "auto"
        assert tool.parallel_safe and not tool.mutates
        assert tool.path_fields == ("path",)

    def test_classified_like_fs_read_inside_the_wall(self, tmp_path: Path) -> None:
        """Inside the wall fs_grep is static Class A, the same rule as fs_read
        (TD-5141), so it auto-runs with no worker call.  Shell search can
        never be model-granted A (shell-floor), which is what this fixes."""
        registry = create_registry()
        classifier = DecisionClassifier(Boundary(workspace_root=tmp_path))
        grep_req = build_decision_request(
            registry.get("fs_grep"), {"pattern": "x", "path": str(tmp_path / "src")}
        )
        read_req = build_decision_request(
            registry.get("fs_read"), {"path": str(tmp_path / "src" / "a.py")}
        )
        for req in (grep_req, read_req):
            decision = classifier.classify(req)
            assert decision.decision_class is DecisionClass.A
            assert decision.rule is not None and decision.rule.id == "builtin-read"
        shell_req = build_decision_request(registry.get("shell"), {"command": "grep -rn x src"})
        assert classifier.classify(shell_req).decision_class is DecisionClass.B

    async def test_policy_rule_lets_it_run_without_asking(self, tmp_path: Path) -> None:
        from tstd.policy import PolicyConfig, PolicyRule, resolve_explained

        config = PolicyConfig(rules=[PolicyRule(tool="fs_grep", args="**", effect="auto")])
        tool = create_registry().get("fs_grep")
        decision = resolve_explained(
            config, tool, {"pattern": "x", "path": str(tmp_path)}, DecisionClass.B
        )
        assert decision.effect == "auto"

    def test_outside_the_wall_is_not_class_a(self, tmp_path: Path) -> None:
        tool = create_registry().get("fs_grep")
        request = build_decision_request(tool, {"pattern": "x", "path": "/etc"})
        decision = DecisionClassifier(Boundary(workspace_root=tmp_path)).classify(request)
        assert decision.decision_class is DecisionClass.C

    async def test_dispatched_in_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tree(tmp_path, {"docs/a.md": "needle\n"})
        dispatcher = make_dispatcher(tmp_path)
        monkeypatch.chdir(tmp_path.parent)
        result = await dispatcher.dispatch(
            "c1", "fs_grep", {"pattern": "needle", "path": ".", "output_mode": "content"}
        )
        assert result.status == "success", result.output
        assert result.output == "docs/a.md:1: needle"

    async def test_refused_outside_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (tmp_path / "secret.txt").write_text("needle\n")
        dispatcher = make_dispatcher(ws)
        monkeypatch.chdir(ws)
        result = await dispatcher.dispatch("c1", "fs_grep", {"pattern": "needle", "path": ".."})
        assert result.status == "error"
        assert result.error_code == "boundary_refusal"

    def test_worker_child_can_search(self) -> None:
        assert "fs_grep" in WORKER_CHILD_TOOLS

    def test_base_prompt_points_at_the_search_tools(self) -> None:
        assert "fs_grep" in BASE_SYSTEM_PROMPT
        assert "not shell grep" in BASE_SYSTEM_PROMPT


# ── fs_list: path globs, sort, truncation ────────────────────────────────


class TestFsListGlobs:
    async def test_path_glob_implies_recursive(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"src/a.ts": "", "src/x/b.ts": "", "src/x/c.py": "", "lib/d.ts": ""})
        out = await fs_list(None, str(tmp_path), pattern="src/**/*.ts")
        assert out == "src/a.ts\nsrc/x/b.ts"

    async def test_name_glob_without_recursive_stays_shallow(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"a.py": "", "pkg/b.py": ""})
        assert await fs_list(None, str(tmp_path), pattern="*.py") == "a.py"
        assert (
            await fs_list(None, str(tmp_path), pattern="*.py", recursive=True) == "a.py\npkg/b.py"
        )

    async def test_sort_by_modified_newest_first(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"old.txt": "", "new.txt": "", "mid.txt": ""})
        for i, name in enumerate(["old.txt", "mid.txt", "new.txt"]):
            os.utime(tmp_path / name, (1_000_000 + i * 100, 1_000_000 + i * 100))
        out = await fs_list(None, str(tmp_path), sort="modified")
        assert out == "new.txt\nmid.txt\nold.txt"

    async def test_bad_sort_is_an_error(self, tmp_path: Path) -> None:
        assert (await fs_list(None, str(tmp_path), sort="size")).startswith("Error: sort")

    def test_truncation_is_stated(self, tmp_path: Path) -> None:
        from tstd.tools.handlers import _list_dir

        _tree(tmp_path, {f"f{i}.txt": "" for i in range(5)})
        out = _list_dir(tmp_path, "*", False, limit=3)
        lines = out.splitlines()
        assert lines[:3] == ["f0.txt", "f1.txt", "f2.txt"]
        assert lines[3] == "… [truncated: showing 3 of 5 entries; narrow the path or pattern]"
