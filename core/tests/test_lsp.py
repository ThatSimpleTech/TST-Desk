"""Language-server diagnostics and navigation (TD-5139).

A small fake server (``tests/fixtures/fake_lsp.py``) runs as a real child
process over stdio, and every call goes through the real dispatcher, so
these exercise the path a session takes: classify, wall, handler, and for
writes the diagnostics folded into the result.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tests.test_dispatch import attach_auto_approver
from tstd.autonomy import AmbiguousClassifier, Boundary, DecisionClassifier
from tstd.autonomy.classifier import LSP_READ_TOOLS, PLAN_READ_ONLY_BUILTINS
from tstd.config import ModelConfig, default_config_yaml
from tstd.lsp import default_pool
from tstd.lsp.diagnostics import new_since, note_for
from tstd.lsp.positions import find_column, from_server_column, to_server_column
from tstd.lsp_config import LspConfig, LspServerConfig
from tstd.tools import ToolDispatcher, create_registry, register_builtin_handlers
from tstd.tools.boundary import PathGuard
from tstd.tools.dispatch import build_decision_request
from tstd.tools.lsp import LSP_TOOL_NAMES

FAKE = Path(__file__).parent / "fixtures" / "fake_lsp.py"

SOURCE = (
    "class Parser:\n    def parse(self):\n        return helper()\n\ndef helper():\n    return 1\n"
)
CALLER = "x = helper()\ny = helper()\n"


@pytest.fixture(autouse=True)
async def _fresh_pool() -> AsyncIterator[None]:
    await default_pool().aclose()
    yield
    await default_pool().aclose()


def _server(log: Path | None = None, **extra: Any) -> LspServerConfig:
    command = [sys.executable, str(FAKE)] + ([str(log)] if log else [])
    fields: dict[str, Any] = {
        "name": "fake",
        "command": command,
        "match": ["*.toy"],
        "language_id": "toy",
        "diagnostics_wait_seconds": 5,
    }
    fields.update(extra)
    return LspServerConfig(**fields)


def _session(workspace: Path, *servers: LspServerConfig, **extra: Any) -> Any:
    config = SimpleNamespace(lsp=LspConfig(servers=list(servers)), hooks=None)
    return SimpleNamespace(workspace_path=str(workspace), config=config, **extra)


async def _never_called(prompt: str) -> str:
    raise AssertionError("the worker tier was asked; the call should be static Class A")


def _dispatcher(workspace: Path) -> ToolDispatcher:
    boundary = Boundary(workspace_root=workspace)
    dispatcher = ToolDispatcher(
        create_registry(lsp_enabled=True),
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(boundary), call_worker=_never_called
        ),
        path_guard=PathGuard(boundary),
    )
    register_builtin_handlers(dispatcher)
    return attach_auto_approver(dispatcher)


async def _call(workspace: Path, session: Any, tool: str, **args: Any) -> Any:
    return await _dispatcher(workspace).dispatch("c1", tool, args, session=session)


# ── AC1: servers configured per language, launched over stdio ──────────


class TestConfig:
    def test_a_server_block_loads_from_config_yaml(self) -> None:
        data = yaml.safe_load(default_config_yaml())
        data["lsp"] = {
            "servers": [
                {
                    "name": "pyright",
                    "command": "pyright-langserver --stdio",
                    "match": ["*.py"],
                    "language_id": "python",
                }
            ]
        }
        config = ModelConfig.model_validate(data)
        [server] = config.lsp.servers
        assert server.command == ["pyright-langserver", "--stdio"]
        assert config.lsp.server_for("src/a.py") is server
        assert config.lsp.server_for("src/a.ts") is None

    def test_the_shipped_config_starts_no_server(self) -> None:
        config = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
        assert config.lsp.servers == []

    @pytest.mark.parametrize(
        "arg", ["--port", "--port=2087", "--socket=4000", "--tcp", "--listen", "--pipe=x"]
    )
    def test_a_socket_or_port_argument_is_refused(self, arg: str) -> None:
        with pytest.raises(ValidationError, match="stdio only"):
            LspServerConfig(command=["server", arg], match=["*.py"], language_id="python")

    def test_without_a_server_the_tools_are_not_offered(self) -> None:
        names = {t.name for t in create_registry().list_tools()}
        assert not names & LSP_TOOL_NAMES
        names = {t.name for t in create_registry(lsp_enabled=True).list_tools()}
        assert names >= LSP_TOOL_NAMES

    async def test_the_server_is_a_stdio_child_with_secrets_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TST_FAKE_API_KEY", "sk-not-for-the-server")
        log = tmp_path.parent / f"{tmp_path.name}-lsp.log"
        (tmp_path / "a.toy").write_text(SOURCE)
        session = _session(tmp_path, _server(log))
        result = await _call(tmp_path, session, "lsp_symbols", path="a.toy")
        assert result.status == "success", result.output
        assert default_pool().running == 1
        methods = log.read_text().split()
        assert methods[:2] == ["initialize", "initialized"]
        assert "saw-a-secret" not in methods
        await default_pool().aclose()
        assert default_pool().running == 0
        assert "shutdown" in log.read_text().split()

    async def test_a_missing_program_is_reported_once(self, tmp_path: Path) -> None:
        server = LspServerConfig(
            name="ghost", command=["no-such-language-server"], match=["*.toy"], language_id="toy"
        )
        session = _session(tmp_path, server)
        first = await _call(tmp_path, session, "fs_write", path="a.toy", content="x\n")
        assert first.status == "success"
        assert "language server `ghost` could not start" in first.output
        again = await _call(tmp_path, session, "fs_write", path="a.toy", content="y\n")
        assert "language server" not in again.output


# ── AC2: new diagnostics for the touched files come back on the result ──


class TestDiagnosticsAfterWrite:
    async def test_fs_write_returns_the_files_problems(self, tmp_path: Path) -> None:
        session = _session(tmp_path, _server())
        result = await _call(
            tmp_path, session, "fs_write", path="a.toy", content="ok\nBAD here\nWARN\n"
        )
        assert result.status == "success"
        assert "language server `fake`: 2 new problem(s) in a.toy" in result.output
        assert "a.toy:2:1 error: bad token (E1)" in result.output
        assert "a.toy:3:1 warning: suspicious token (W1)" in result.output

    async def test_fs_edit_reports_only_what_is_new(self, tmp_path: Path) -> None:
        session = _session(tmp_path, _server())
        await _call(tmp_path, session, "fs_write", path="a.toy", content="BAD one\nok\n")
        edited = await _call(
            tmp_path, session, "fs_edit", path="a.toy", old_string="ok", new_string="WARN"
        )
        assert edited.status == "success", edited.output
        assert "1 new problem(s) in a.toy; 1 reported before still there" in edited.output
        assert "warning: suspicious token" in edited.output
        assert "bad token" not in edited.output

    async def test_fixing_the_last_problem_says_so(self, tmp_path: Path) -> None:
        session = _session(tmp_path, _server())
        await _call(tmp_path, session, "fs_write", path="a.toy", content="BAD\n")
        fixed = await _call(tmp_path, session, "fs_write", path="a.toy", content="good\n")
        assert "no problems left in a.toy" in fixed.output

    async def test_a_clean_file_adds_nothing(self, tmp_path: Path) -> None:
        session = _session(tmp_path, _server())
        result = await _call(tmp_path, session, "fs_write", path="a.toy", content="fine\n")
        assert "language server" not in result.output

    async def test_a_file_no_server_matches_starts_nothing(self, tmp_path: Path) -> None:
        session = _session(tmp_path, _server())
        result = await _call(tmp_path, session, "fs_write", path="a.txt", content="BAD\n")
        assert "language server" not in result.output
        assert default_pool().running == 0

    async def test_an_autonomous_session_starts_no_server_on_the_host(self, tmp_path: Path) -> None:
        session = _session(tmp_path, _server(), autonomy=True)
        result = await _call(tmp_path, session, "fs_write", path="a.toy", content="BAD\n")
        assert result.status == "success"
        assert "language server" not in result.output
        assert default_pool().running == 0

    def test_new_since_counts_duplicates_and_ignores_line_moves(self) -> None:
        def d(line: int, message: str) -> dict[str, Any]:
            return {"range": {"start": {"line": line, "character": 0}}, "message": message}

        before = [d(1, "a")]
        assert new_since([d(5, "a")], before) == []
        assert new_since([d(5, "a"), d(6, "a")], before) == [d(6, "a")]
        assert note_for("x", "f", [d(1, "a")], [d(1, "a")], 20) == ""


# ── AC3: Class A tools for definition, references and symbols ──────────


class TestNavigation:
    async def test_definition_by_symbol_name(self, tmp_path: Path) -> None:
        (tmp_path / "lib.toy").write_text(SOURCE)
        (tmp_path / "use.toy").write_text(CALLER)
        session = _session(tmp_path, _server())
        await _call(tmp_path, session, "lsp_symbols", path="lib.toy")  # opens lib.toy
        result = await _call(
            tmp_path, session, "lsp_definition", path="use.toy", line=2, symbol="helper"
        )
        assert result.status == "success", result.output
        assert result.decision_class == "A"
        assert "- lib.toy:5:5: def helper():" in result.output

    async def test_references_across_files(self, tmp_path: Path) -> None:
        (tmp_path / "lib.toy").write_text(SOURCE)
        (tmp_path / "use.toy").write_text(CALLER)
        session = _session(tmp_path, _server())
        await _call(tmp_path, session, "lsp_symbols", path="use.toy")
        result = await _call(
            tmp_path, session, "lsp_references", path="lib.toy", line=5, symbol="helper"
        )
        assert result.status == "success", result.output
        assert result.decision_class == "A"
        assert "4 reference(s) to helper" in result.output
        assert "- use.toy:1:5: x = helper()" in result.output
        assert "- lib.toy:3:16: return helper()" in result.output

    async def test_document_symbols_nest(self, tmp_path: Path) -> None:
        (tmp_path / "lib.toy").write_text(SOURCE)
        session = _session(tmp_path, _server())
        result = await _call(tmp_path, session, "lsp_symbols", path="lib.toy")
        assert result.status == "success", result.output
        assert result.decision_class == "A"
        assert result.output.splitlines()[1:] == [
            "- class Parser (line 1)",
            "  - method parse (line 2)",
            "- function helper (line 5)",
        ]

    async def test_workspace_symbol_search(self, tmp_path: Path) -> None:
        (tmp_path / "lib.toy").write_text(SOURCE)
        session = _session(tmp_path, _server())
        result = await _call(tmp_path, session, "lsp_symbols", path="lib.toy", query="help")
        assert result.status == "success", result.output
        assert "function helper lib.toy:5:5: def helper():" in result.output

    async def test_a_symbol_not_on_the_line_is_explained(self, tmp_path: Path) -> None:
        (tmp_path / "lib.toy").write_text(SOURCE)
        session = _session(tmp_path, _server())
        result = await _call(
            tmp_path, session, "lsp_definition", path="lib.toy", line=1, symbol="helper"
        )
        assert result.status == "error"
        assert result.error_code == "lsp_bad_position"
        assert "class Parser:" in result.output

    async def test_no_server_for_the_file_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n")
        session = _session(tmp_path, _server())
        result = await _call(tmp_path, session, "lsp_symbols", path="a.py")
        assert result.status == "error"
        assert result.error_code == "lsp_unavailable"

    async def test_a_path_outside_the_workspace_is_class_c(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / f"{tmp_path.name}-out.toy"
        outside.write_text(SOURCE)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session = _session(workspace, _server())
        result = await _call(workspace, session, "lsp_symbols", path=str(outside))
        assert result.status == "error"
        assert result.decision_class == "C"
        assert default_pool().running == 0

    @pytest.mark.parametrize("name", sorted(LSP_TOOL_NAMES))
    def test_each_tool_is_static_a_inside_the_wall(self, tmp_path: Path, name: str) -> None:
        (tmp_path / "a.toy").write_text("x\n")
        tool = create_registry(lsp_enabled=True).require(name)
        request = build_decision_request(tool, {"path": str(tmp_path / "a.toy"), "line": 1})
        decision = DecisionClassifier(Boundary(workspace_root=tmp_path)).classify(request)
        assert decision.decision_class == "A"
        assert decision.rule is not None and decision.rule.id == "lsp-read"

    def test_the_classifier_and_the_tools_agree_on_names(self) -> None:
        assert LSP_READ_TOOLS == LSP_TOOL_NAMES
        assert LSP_TOOL_NAMES <= PLAN_READ_ONLY_BUILTINS


class TestColumns:
    def test_utf16_columns_count_surrogate_pairs(self) -> None:
        line = "s = '😀' + name"
        col = line.index("name")
        assert to_server_column(line, col, "utf-16") == col + 1
        assert from_server_column(line, col + 1, "utf-16") == col
        assert to_server_column(line, col, "utf-8") == col + 3
        assert from_server_column(line, col + 3, "utf-8") == col

    def test_find_column_prefers_a_whole_word(self) -> None:
        assert find_column("helpers = helper()", "helper") == 10
        assert find_column("nothing here", "helper") is None
