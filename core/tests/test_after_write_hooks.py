"""After-write hooks: a formatter or linter runs on the agent's writes (TD-5135).

The hooks here are small Python scripts run through the real dispatcher,
so each test exercises the same path a session's ``fs_write`` and
``fs_edit`` take: classify, write, hook, diff, result.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tests.test_read_tools import make_dispatcher
from tstd.after_write import build_argv, clip_output, matches, session_hooks
from tstd.config import AfterWriteHook, HooksConfig, ModelConfig, default_config_yaml
from tstd.protocol import HookRun
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.workspace_trust import TrustStore, read_view

# A linter that fails on any line holding "BAD", naming the line.
LINT = textwrap.dedent(
    """
    import sys
    bad = False
    for name in sys.argv[1:]:
        for n, line in enumerate(open(name), 1):
            if "BAD" in line:
                print(f"{name}:{n}: E999 found BAD")
                bad = True
    sys.exit(1 if bad else 0)
    """
)

# A formatter that strips trailing spaces in place.
FORMAT = textwrap.dedent(
    """
    import sys
    for name in sys.argv[1:]:
        text = open(name).read()
        fixed = "\\n".join(l.rstrip() for l in text.split("\\n"))
        if fixed != text:
            open(name, "w").write(fixed)
    """
)

SLEEP = "import time; time.sleep(30)"


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path.parent / f"{tmp_path.name}-{name}.py"
    path.write_text(body)
    return str(path)


def _hook(tmp_path: Path, name: str, body: str, **extra: Any) -> AfterWriteHook:
    return AfterWriteHook(
        name=name, command=[sys.executable, _script(tmp_path, name, body)], **extra
    )


def _session(workspace: Path, *hooks: AfterWriteHook, trust: TrustStore | None = None) -> Any:
    config = SimpleNamespace(hooks=HooksConfig(after_write=list(hooks)))
    return SimpleNamespace(workspace_path=str(workspace), config=config, trust_store=trust)


async def _write(workspace: Path, session: Any, path: str, content: str) -> Any:
    dispatcher = make_dispatcher(workspace)
    return await dispatcher.dispatch(
        "c1", "fs_write", {"path": path, "content": content}, session=session
    )


# ── AC1: a configured command runs on the files fs_write/fs_edit changed ──


class TestFailuresGoBackToTheModel:
    async def test_a_lint_failure_is_in_the_tool_result(self, tmp_path: Path) -> None:
        session = _session(tmp_path, _hook(tmp_path, "lint", LINT))
        result = await _write(tmp_path, session, "a.py", "ok = 1\nBAD = 2\n")
        assert result.status == "success"  # the write itself happened
        assert (tmp_path / "a.py").read_text() == "ok = 1\nBAD = 2\n"
        assert "after-write hook `lint` failed: exit 1" in result.output
        assert "a.py:2: E999 found BAD" in result.output
        [run] = result.hooks
        assert run.name == "lint"
        assert run.status == "failed"
        assert run.exit_code == 1
        assert run.duration_ms >= 0

    async def test_a_clean_pass_adds_nothing_for_the_model(self, tmp_path: Path) -> None:
        session = _session(tmp_path, _hook(tmp_path, "lint", LINT))
        result = await _write(tmp_path, session, "a.py", "ok = 1\n")
        assert "after-write" not in result.output
        assert [r.status for r in result.hooks] == ["passed"]

    async def test_fs_edit_runs_the_hook_too(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("ok = 1\n")
        session = _session(tmp_path, _hook(tmp_path, "lint", LINT))
        dispatcher = make_dispatcher(tmp_path)
        result = await dispatcher.dispatch(
            "c1",
            "fs_edit",
            {"path": "a.py", "old_string": "ok = 1", "new_string": "BAD = 1"},
            session=session,
        )
        assert result.status == "success"
        assert "found BAD" in result.output
        assert [r.status for r in result.hooks] == ["failed"]

    async def test_a_formatter_change_is_in_the_diff_and_named(self, tmp_path: Path) -> None:
        session = _session(tmp_path, _hook(tmp_path, "fmt", FORMAT))
        result = await _write(tmp_path, session, "a.py", "x = 1   \n")
        assert (tmp_path / "a.py").read_text() == "x = 1\n"
        [run] = result.hooks
        assert run.status == "passed" and run.changed
        assert "`fmt` changed a.py; read it again" in result.output
        # The diff is taken after the hook: it shows the formatted file.
        assert result.diff is not None
        assert "+x = 1\n" in result.diff + "\n" and "x = 1   " not in result.diff

    async def test_a_hook_that_hangs_is_stopped(self, tmp_path: Path) -> None:
        hook = _hook(tmp_path, "slow", SLEEP, timeout_seconds=0.3)
        result = await _write(tmp_path, _session(tmp_path, hook), "a.py", "x = 1\n")
        assert result.status == "success"
        assert [r.status for r in result.hooks] == ["timed_out"]
        assert "`slow` stopped after 0.3 s" in result.output

    async def test_a_missing_program_is_reported(self, tmp_path: Path) -> None:
        hook = AfterWriteHook(name="gone", command=["tst-no-such-formatter-5135", "{files}"])
        result = await _write(tmp_path, _session(tmp_path, hook), "a.py", "x = 1\n")
        assert result.status == "success"
        assert [r.status for r in result.hooks] == ["error"]
        assert "could not start: tst-no-such-formatter-5135 is not on PATH" in result.output

    async def test_only_matching_files_run_the_hook(self, tmp_path: Path) -> None:
        hook = _hook(tmp_path, "lint", LINT, match=["*.py"])
        result = await _write(tmp_path, _session(tmp_path, hook), "notes.md", "BAD\n")
        assert result.hooks == ()
        assert "after-write" not in result.output

    async def test_other_tools_never_run_hooks(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("BAD\n")
        session = _session(tmp_path, _hook(tmp_path, "lint", LINT))
        result = await make_dispatcher(tmp_path).dispatch(
            "c1", "fs_read", {"path": "a.py"}, session=session
        )
        assert result.hooks == ()

    async def test_hooks_run_in_order_and_all_report(self, tmp_path: Path) -> None:
        session = _session(tmp_path, _hook(tmp_path, "fmt", FORMAT), _hook(tmp_path, "lint", LINT))
        result = await _write(tmp_path, session, "a.py", "BAD = 1  \n")
        assert [(r.name, r.status) for r in result.hooks] == [
            ("fmt", "passed"),
            ("lint", "failed"),
        ]

    async def test_no_hooks_configured_changes_nothing(self, tmp_path: Path) -> None:
        result = await _write(tmp_path, _session(tmp_path), "a.py", "x = 1\n")
        assert result.hooks == ()
        assert result.output.startswith("wrote ")


# ── The workspace's hooks wait for trust (TD-5113, TD-5134 decision) ─────


def _workspace_hook(tmp_path: Path) -> None:
    script = _script(tmp_path, "lint", LINT)
    (tmp_path / ".tst").mkdir()
    (tmp_path / ".tst" / "config.yaml").write_text(
        yaml.safe_dump(
            {"hooks": {"after_write": [{"name": "repo-lint", "command": [sys.executable, script]}]}}
        )
    )


class TestWorkspaceHooks:
    async def test_an_untrusted_workspace_hook_never_runs(self, tmp_path: Path) -> None:
        _workspace_hook(tmp_path)
        store = TrustStore(tmp_path.parent / f"{tmp_path.name}-data")
        assert read_view(tmp_path, store).state == "pending"
        session = SimpleNamespace(workspace_path=str(tmp_path), config=None, trust_store=store)
        result = await _write(tmp_path, session, "a.py", "BAD\n")
        assert result.hooks == ()

    async def test_a_trusted_workspace_hook_runs(self, tmp_path: Path) -> None:
        _workspace_hook(tmp_path)
        store = TrustStore(tmp_path.parent / f"{tmp_path.name}-data")
        view = read_view(tmp_path, store)
        assert view.sha256 is not None
        store.record(tmp_path, view.sha256, "trusted")
        session = SimpleNamespace(workspace_path=str(tmp_path), config=None, trust_store=store)
        result = await _write(tmp_path, session, "a.py", "BAD\n")
        assert [(r.name, r.status) for r in result.hooks] == [("repo-lint", "failed")]

    def test_user_hooks_come_before_the_workspace_ones(self, tmp_path: Path) -> None:
        _workspace_hook(tmp_path)
        session = _session(tmp_path, AfterWriteHook(name="mine", command=["true"]))
        assert [h.label for h in session_hooks(session)] == ["mine", "repo-lint"]

    def test_a_bad_workspace_section_applies_nothing(self, tmp_path: Path) -> None:
        (tmp_path / ".tst").mkdir()
        (tmp_path / ".tst" / "config.yaml").write_text("hooks: {after_write: [{name: x}]}\n")
        session = SimpleNamespace(workspace_path=str(tmp_path), config=None, trust_store=None)
        assert session_hooks(session) == []


# ── Config shape ─────────────────────────────────────────────────────────


class TestConfig:
    def test_a_string_command_is_split_into_words(self) -> None:
        hook = AfterWriteHook(command="npx prettier --write {files}")  # type: ignore[arg-type]
        assert hook.command == ["npx", "prettier", "--write", "{files}"]
        assert hook.label == "npx"

    def test_files_replace_the_token_or_go_on_the_end(self) -> None:
        with_token = AfterWriteHook(command=["ruff", "check", "{files}", "--quiet"])
        assert build_argv(with_token, ["a.py"]) == ["ruff", "check", "a.py", "--quiet"]
        without = AfterWriteHook(command=["ruff", "format"])
        assert build_argv(without, ["a.py", "b.py"]) == ["ruff", "format", "a.py", "b.py"]

    @pytest.mark.parametrize("command", [[], [""], ["{files}"], "  "])
    def test_a_command_needs_a_program(self, command: Any) -> None:
        with pytest.raises(ValidationError):
            AfterWriteHook(command=command)

    def test_unknown_keys_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            HooksConfig.model_validate({"after_write": [{"command": ["x"], "shell": True}]})
        with pytest.raises(ValidationError):
            HooksConfig.model_validate({"before_write": []})

    def test_name_patterns_match_the_file_name_path_patterns_the_path(self) -> None:
        hook = AfterWriteHook(command=["x"], match=["*.py", "docs/*.md"])
        assert matches(hook, "src/deep/a.py")
        assert matches(hook, "docs/a.md")
        assert not matches(hook, "README.md")

    def test_the_shipped_config_has_no_hooks(self) -> None:
        shipped = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
        assert shipped.hooks.after_write == []
        assert ModelConfig.model_fields["hooks"].annotation is HooksConfig

    def test_output_keeps_its_start_and_end(self) -> None:
        text = "HEAD" + "x" * 5000 + "TAIL"
        clipped = clip_output(text)
        assert clipped.startswith("HEAD") and clipped.endswith("TAIL")
        assert len(clipped) < 2100


# ── AC3: the time shows in the step timeline (the tool_result event) ─────


def test_the_tool_result_event_carries_each_hook_and_its_time() -> None:
    event = ToolResultEvent(
        session_id="s",
        tool_call_id="c1",
        status="success",
        output="wrote 1 bytes",
        hooks=[HookRun(name="ruff", status="failed", exit_code=1, duration_ms=420)],
        seq=1,
    )
    wire = event.model_dump()
    assert wire["hooks"] == [
        {"name": "ruff", "status": "failed", "exit_code": 1, "duration_ms": 420, "changed": False}
    ]
    assert (
        ToolResultEvent(session_id="s", tool_call_id="c", status="success", output="", seq=1).hooks
        == []
    )


async def test_a_session_turn_logs_the_hooks_on_its_tool_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real loop: the event the window reads carries the run."""
    import json

    from tests.test_dispatch import make_config, start_loop, wait_for_turn
    from tstd.mock import MockProvider, Script
    from tstd.router import TierRouter
    from tstd.session import Session

    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = Session(str(workspace))
    config = make_config().model_copy(
        update={"hooks": HooksConfig(after_write=[_hook(tmp_path, "lint", LINT)])}
    )
    session.config = config
    dispatcher = make_dispatcher(workspace)
    monkeypatch.chdir(workspace)
    args = json.dumps({"path": "a.py", "content": "BAD = 1\n"})
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(kind="tool_call", tool_name="fs_write", tool_arguments=args),
                Script(kind="stream", content="Done"),
            ]
        }
    )
    runner = await start_loop(
        session, TierRouter(lead_turns=3), mock, config, dispatcher.registry, dispatcher
    )
    await session.add_user_message("Write it")
    await wait_for_turn(session, 1)
    [event] = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
    assert [(h.name, h.status) for h in event.hooks] == [("lint", "failed")]
    assert "found BAD" in event.output
    await runner.cancel()
