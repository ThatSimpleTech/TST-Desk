"""Human-path charter editor (TD-4002).

``get_charter`` / ``save_charter`` write ``.tst/autonomy/CHARTER.md`` as
the human.  The agent cannot invoke them as tools.  ``source_of_truth``
is workspace-walled through PathGuard.  The write does not commit.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_charter import VALID_FRONTMATTER, VALID_YAML_ONLY, _write_charter
from tests.test_checkpoint import _git, make_repo
from tstd.autonomy.charter import CharterError, charter_path, parse_charter
from tstd.autonomy.charter_io import (
    assert_sources_walled,
    read_charter_document,
    write_charter_document,
)
from tstd.daemon import Daemon
from tstd.tools.registry import create_registry

_VALID: dict[str, object] = {
    "objective": "Ship the CSV importer end to end.",
    "definition_of_done": [
        "Every fixture in tests/fixtures/import round-trips byte-identically.",
    ],
    "source_of_truth": ["docs/spec.md"],
    "boundary": {
        "writable_paths": ["src/**", "tests/**"],
        "allowed_commands": ["cargo", "pytest"],
        "network": "deny",
    },
    "caps": {"spend_usd": 3.0, "wall_clock_hours": 8.0, "max_iterations": 40},
    "stop_conditions": [],
}


def _get(workspace: Path) -> str:
    return json.dumps({"type": "get_charter", "workspace_path": str(workspace)})


def _save(
    workspace: Path,
    charter: dict[str, object] | None = None,
    notes: str = "",
) -> str:
    return json.dumps(
        {
            "type": "save_charter",
            "workspace_path": str(workspace),
            "charter": charter if charter is not None else _VALID,
            "notes": notes,
        }
    )


class TestIo:
    def test_absent_is_not_present(self, tmp_path: Path) -> None:
        present, charter, notes = read_charter_document(tmp_path)
        assert present is False
        assert charter is None
        assert notes == ""

    def test_reads_frontmatter_and_notes(self, tmp_path: Path) -> None:
        _write_charter(tmp_path, VALID_FRONTMATTER)
        present, charter, notes = read_charter_document(tmp_path)
        assert present is True
        assert charter is not None
        assert charter.objective.startswith("Ship the CSV")
        assert "Human context" in notes

    def test_write_parses_back(self, tmp_path: Path) -> None:
        written, notes = write_charter_document(tmp_path, _VALID, "Pane notes.")
        assert written.objective == _VALID["objective"]
        assert notes == "Pane notes."
        text = charter_path(tmp_path).read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert parse_charter(text) == written

    def test_unknown_key_names_the_field(self, tmp_path: Path) -> None:
        bad = dict(_VALID)
        bad["definiton_of_done"] = ["typo"]
        try:
            write_charter_document(tmp_path, bad)
        except CharterError as e:
            assert "definiton_of_done" in str(e)
        else:
            raise AssertionError("expected CharterError")
        assert not charter_path(tmp_path).exists()

    def test_missing_objective_named(self, tmp_path: Path) -> None:
        bad = dict(_VALID)
        del bad["objective"]
        try:
            write_charter_document(tmp_path, bad)
        except CharterError as e:
            assert "objective" in str(e)
        else:
            raise AssertionError("expected CharterError")

    def test_sot_absolute_refused(self, tmp_path: Path) -> None:
        bad = dict(_VALID)
        bad["source_of_truth"] = ["/etc/passwd"]
        try:
            write_charter_document(tmp_path, bad)
        except CharterError as e:
            assert "source_of_truth" in str(e)
        else:
            raise AssertionError("expected CharterError")
        assert not charter_path(tmp_path).exists()

    def test_sot_dotdot_refused(self, tmp_path: Path) -> None:
        bad = dict(_VALID)
        bad["source_of_truth"] = ["../secret.md"]
        try:
            write_charter_document(tmp_path, bad)
        except CharterError as e:
            assert "source_of_truth" in str(e)
        else:
            raise AssertionError("expected CharterError")

    def test_sot_symlink_out_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "leak.md"
        target.write_text("no\n", encoding="utf-8")
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "docs").mkdir()
        link = ws / "docs" / "spec.md"
        link.symlink_to(target)
        bad = dict(_VALID)
        bad["source_of_truth"] = ["docs/spec.md"]
        try:
            write_charter_document(ws, bad)
        except CharterError as e:
            assert "source_of_truth" in str(e)
        else:
            raise AssertionError("expected CharterError")

    def test_sot_inside_allowed_even_if_missing(self, tmp_path: Path) -> None:
        write_charter_document(tmp_path, _VALID)
        present, charter, _ = read_charter_document(tmp_path)
        assert present is True
        assert charter is not None
        assert charter.source_of_truth == ["docs/spec.md"]
        assert_sources_walled(tmp_path, charter)

    def test_write_does_not_commit(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = _git(repo, "rev-parse", "HEAD")
        write_charter_document(repo, _VALID)
        assert _git(repo, "rev-parse", "HEAD") == before
        status = _git(repo, "status", "--porcelain")
        assert ".tst" in status
        assert charter_path(repo).is_file()


class TestDaemon:
    async def test_get_absent(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = await daemon._handle_message(_get(tmp_path), None)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["type"] == "charter"
        assert payload["present"] is False
        assert payload["charter"] is None
        await daemon._shutdown()

    async def test_get_existing(self, tmp_path: Path) -> None:
        _write_charter(tmp_path, VALID_YAML_ONLY)
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = await daemon._handle_message(_get(tmp_path), None)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["present"] is True
        assert payload["charter"]["objective"].startswith("Ship the CSV")
        await daemon._shutdown()

    async def test_get_invalid_names_the_field(self, tmp_path: Path) -> None:
        _write_charter(tmp_path, "definition_of_done: [done]\n")
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = await daemon._handle_message(_get(tmp_path), None)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["type"] == "error"
        assert payload["code"] == "invalid_charter"
        assert "objective" in payload["message"]
        await daemon._shutdown()

    async def test_save_writes_and_replies(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = await daemon._handle_message(_save(tmp_path, notes="From the pane."), None)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["type"] == "charter"
        assert payload["present"] is True
        assert payload["notes"] == "From the pane."
        assert payload["charter"]["source_of_truth"] == ["docs/spec.md"]
        text = charter_path(tmp_path).read_text(encoding="utf-8")
        assert "Ship the CSV importer" in text
        await daemon._shutdown()

    async def test_save_refuses_sot_escape(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        bad = dict(_VALID)
        bad["source_of_truth"] = ["../secret.md"]
        raw = await daemon._handle_message(_save(tmp_path, bad), None)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["type"] == "error"
        assert payload["code"] == "invalid_charter"
        assert "source_of_truth" in payload["message"]
        assert not charter_path(tmp_path).exists()
        await daemon._shutdown()

    async def test_save_refuses_unknown_key(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        bad = dict(_VALID)
        bad["version"] = 1
        raw = await daemon._handle_message(_save(tmp_path, bad), None)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["code"] == "invalid_charter"
        assert "version" in payload["message"]
        await daemon._shutdown()

    async def test_save_missing_workspace(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = await daemon._handle_message(_save(tmp_path / "nope"), None)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["code"] == "workspace_not_found"
        await daemon._shutdown()

    async def test_save_does_not_commit(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = _git(repo, "rev-parse", "HEAD")
        daemon = Daemon(data_dir=tmp_path / "data")
        await daemon._handle_message(_save(repo), None)
        assert _git(repo, "rev-parse", "HEAD") == before
        await daemon._shutdown()

    def test_not_in_tool_registry(self) -> None:
        names = {tool.name for tool in create_registry().list_tools()}
        assert "get_charter" not in names
        assert "save_charter" not in names

    async def test_dispatcher_rejects_as_unknown_tool(self, tmp_path: Path) -> None:
        from tests.test_security_suite import make_dispatcher

        dispatcher = make_dispatcher(tmp_path)
        result = await dispatcher.dispatch(
            "t1",
            "save_charter",
            {"workspace_path": str(tmp_path), "charter": _VALID},
        )
        assert result.status == "error"
        assert result.error_code == "unknown_tool"
