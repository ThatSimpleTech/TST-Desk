"""Tests for the signed charter (TD-4001) — spec §12.4 validation and
the git-tracked start gate.

Covers: frontmatter and raw-YAML parsing, refusals that name the
offending field, body prose being ignored, ``load_charter`` on a
workspace without a charter, and ``charter_start_error`` across the git
matrix (committed / modified / staged / untracked / no repo).  The
classifier's Class C treatment of the charter lives in
``test_classifier.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_checkpoint import make_repo
from tstd.autonomy.charter import (
    CHARTER_RELATIVE_PARTS,
    MAX_CHARTER_BYTES,
    Charter,
    CharterError,
    charter_path,
    charter_start_error,
    load_charter,
    parse_charter,
)

FULL_SPEC_YAML = """\
objective: Ship the CSV importer end to end.
definition_of_done:
  - Every fixture in tests/fixtures/import round-trips byte-identically.
  - The full test suite passes green.
source_of_truth:
  - docs/spec.md section 7
boundary:
  writable_paths:
    - src/**
    - tests/**
  allowed_commands:
    - cargo
    - pytest
  network: deny
caps:
  spend_usd: 12.5
  wall_clock_hours: 6.0
  max_iterations: 80
stop_conditions:
  - Two consecutive failures on the same test.
"""

VALID_FRONTMATTER = (
    f"---\n{FULL_SPEC_YAML}---\n\n"
    "Human context: this charter backs the Q3 importer push. Prose below the\n"
    "closing fence is notes for people; the parser ignores it.\n"
)

VALID_YAML_ONLY = """\
objective: Ship the CSV importer end to end.
definition_of_done:
  - Every fixture in tests/fixtures/import round-trips byte-identically.
source_of_truth: []
boundary:
  writable_paths:
    - src/**
    - tests/**
  allowed_commands:
    - cargo
    - pytest
  network: deny
caps:
  spend_usd: 3.0
  max_iterations: 40
"""


def _write_charter(ws: Path, text: str) -> Path:
    path = charter_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _stage_all(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)


def _commit_all(repo: Path, message: str) -> None:
    _stage_all(repo)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=True)


def _skip_worktree(repo: Path, rel: str) -> None:
    subprocess.run(["git", "-C", str(repo), "update-index", "--skip-worktree", rel], check=True)


def make_chartered_repo(tmp_path: Path) -> Path:
    """A repo on ``main`` with a valid, committed charter."""
    repo = make_repo(tmp_path)
    _write_charter(repo, VALID_YAML_ONLY)
    _commit_all(repo, "sign the charter")
    return repo


# ── Parsing: spec §12.4 shape ───────────────────────────────────────────


class TestParse:
    def test_spec_example_frontmatter_round_trips(self) -> None:
        charter = parse_charter(VALID_FRONTMATTER)
        assert isinstance(charter, Charter)
        assert charter.objective == "Ship the CSV importer end to end."
        assert charter.definition_of_done == [
            "Every fixture in tests/fixtures/import round-trips byte-identically.",
            "The full test suite passes green.",
        ]
        assert charter.source_of_truth == ["docs/spec.md section 7"]
        assert charter.boundary.writable_paths == ["src/**", "tests/**"]
        assert charter.boundary.allowed_commands == ["cargo", "pytest"]
        assert charter.boundary.network == "deny"
        assert charter.caps.spend_usd == 12.5
        assert charter.caps.wall_clock_hours == 6.0
        assert charter.caps.max_iterations == 80
        assert charter.stop_conditions == ["Two consecutive failures on the same test."]

    def test_raw_yaml_whole_file_parses(self) -> None:
        charter = parse_charter(VALID_YAML_ONLY)
        assert charter.objective == "Ship the CSV importer end to end."
        assert charter.source_of_truth == []
        assert charter.stop_conditions == []  # optional lists default empty
        assert charter.boundary.writable_paths == ["src/**", "tests/**"]
        assert charter.caps.max_iterations == 40

    def test_body_prose_is_ignored(self) -> None:
        fenced_only = f"---\n{FULL_SPEC_YAML}---\n"
        # Same frontmatter, plus a body — must parse to exactly the same
        # charter as the block alone.
        assert parse_charter(VALID_FRONTMATTER) == parse_charter(fenced_only)

    def test_objective_is_stripped(self) -> None:
        charter = parse_charter(
            "objective: '  find the bug  '\ndefinition_of_done: [done]\n"
            "boundary:\n  network: deny\ncaps:\n  spend_usd: 1\n"
        )
        assert charter.objective == "find the bug"

    def test_empty_text_names_required_fields(self) -> None:
        for text in ("", "---\n# nothing yet\n---\nsome prose\n"):
            with pytest.raises(CharterError) as ei:
                parse_charter(text)
            assert "objective" in str(ei.value)
            assert "definition_of_done" in str(ei.value)

    def test_invalid_yaml_rejected(self) -> None:
        with pytest.raises(CharterError):
            parse_charter("objective: [unclosed\n")

    def test_non_mapping_yaml_rejected(self) -> None:
        with pytest.raises(CharterError):
            parse_charter("- just\n- a\n- list\n")


# ── Refusals name the offending field ──────────────────────────────────


class TestFieldRefusals:
    def test_missing_objective_named(self) -> None:
        with pytest.raises(CharterError) as ei:
            parse_charter("definition_of_done: [done]\n")
        assert "objective" in str(ei.value)

    def test_empty_definition_of_done_refused(self) -> None:
        # A run with no DoD can never stop.
        with pytest.raises(CharterError) as ei:
            parse_charter("objective: x\ndefinition_of_done: []\n")
        assert "definition_of_done" in str(ei.value)

    def test_blank_dod_entry_refused(self) -> None:
        with pytest.raises(CharterError) as ei:
            parse_charter("objective: x\ndefinition_of_done: ['  ']\n")
        assert "definition_of_done" in str(ei.value)

    def test_typo_key_refused_with_its_name(self) -> None:
        # extra="forbid": a misspelled key fails loudly instead of
        # silently dropping the constraint it meant to carry.
        with pytest.raises(CharterError) as ei:
            parse_charter(
                "objective: x\ndefiniton_of_done:\n  - done\ndefinition_of_done: [done]\n"
                "boundary:\n  network: deny\ncaps:\n  spend_usd: 1\n"
            )
        assert "definiton_of_done" in str(ei.value)

    def test_bad_network_value_named(self) -> None:
        with pytest.raises(CharterError) as ei:
            parse_charter("objective: x\ndefinition_of_done: [done]\nboundary:\n  network: maybe\n")
        assert "network" in str(ei.value)

    def test_negative_spend_named(self) -> None:
        with pytest.raises(CharterError) as ei:
            parse_charter(
                "objective: x\ndefinition_of_done: [done]\n"
                "boundary:\n  network: deny\ncaps:\n  spend_usd: -5\n"
            )
        assert "spend_usd" in str(ei.value)

    def test_blank_stop_condition_entry_named(self) -> None:
        with pytest.raises(CharterError) as ei:
            parse_charter(
                "objective: x\ndefinition_of_done: [done]\nstop_conditions: ['']\n"
                "boundary:\n  network: deny\ncaps:\n  spend_usd: 1\n"
            )
        assert "stop_conditions" in str(ei.value)

    def test_missing_boundary_section_named(self) -> None:
        # Required: an omitted section must not default to the loosest
        # wall on the shelf.
        with pytest.raises(CharterError) as ei:
            parse_charter("objective: x\ndefinition_of_done: [done]\ncaps:\n  spend_usd: 1\n")
        assert "boundary" in str(ei.value)

    def test_missing_caps_section_named(self) -> None:
        with pytest.raises(CharterError) as ei:
            parse_charter("objective: x\ndefinition_of_done: [done]\nboundary:\n  network: deny\n")
        assert "caps" in str(ei.value)

    def test_typo_inside_boundary_refused_with_its_name(self) -> None:
        # The shared sections forbid unknown keys too: a typo there must
        # not silently revert that one constraint to its default.
        with pytest.raises(CharterError) as ei:
            parse_charter(
                "objective: x\ndefinition_of_done: [done]\n"
                "boundary:\n  writable_path:\n    - src/**\ncaps:\n  spend_usd: 1\n"
            )
        assert "writable_path" in str(ei.value)

    def test_typo_inside_caps_refused_with_its_name(self) -> None:
        with pytest.raises(CharterError) as ei:
            parse_charter(
                "objective: x\ndefinition_of_done: [done]\n"
                "boundary:\n  network: deny\ncaps:\n  spend: 5\n"
            )
        assert "spend" in str(ei.value)

    def test_duplicate_top_level_key_refused(self) -> None:
        # A signed contract may not last-win silently on a repeated key.
        with pytest.raises(CharterError) as ei:
            parse_charter(
                "objective: first\nobjective: second\n"
                "definition_of_done: [done]\nboundary:\n  network: deny\ncaps:\n  spend_usd: 1\n"
            )
        assert "duplicate" in str(ei.value)
        assert "objective" in str(ei.value)

    def test_duplicate_nested_key_refused(self) -> None:
        with pytest.raises(CharterError) as ei:
            parse_charter(
                "objective: x\ndefinition_of_done: [done]\n"
                "boundary:\n  network: deny\n  network: allow\ncaps:\n  spend_usd: 1\n"
            )
        assert "duplicate" in str(ei.value)

    def test_indented_fence_line_is_content_not_a_closer(self) -> None:
        # An indented --- inside a block scalar is charter text; treating
        # it as the closing fence would silently truncate the objective.
        text = (
            "---\n"
            "objective: |\n"
            "  step one\n"
            "  ---\n"
            "  step two\n"
            "definition_of_done: [done]\n"
            "boundary:\n  network: deny\n"
            "caps:\n  spend_usd: 1\n"
            "---\n"
            "Free-form prose.\n"
        )
        charter = parse_charter(text)
        assert charter.objective == "step one\n---\nstep two"

    async def test_bom_frontmatter_loads(self, tmp_path: Path) -> None:
        # Windows-authored files may carry a BOM; it must not turn valid
        # YAML into a cryptic parse failure.
        path = charter_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xef\xbb\xbf" + VALID_YAML_ONLY.encode("utf-8"))
        charter = await load_charter(tmp_path)
        assert charter.objective == "Ship the CSV importer end to end."

    async def test_oversized_charter_refused_without_parsing(self, tmp_path: Path) -> None:
        path = charter_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# padding\n" * (MAX_CHARTER_BYTES // 10 + 1), encoding="utf-8")
        with pytest.raises(CharterError) as ei:
            await load_charter(tmp_path)
        assert str(MAX_CHARTER_BYTES) in str(ei.value)


# ── load_charter ────────────────────────────────────────────────────────


class TestLoadCharter:
    async def test_absent_charter_raises_naming_the_path(self, tmp_path: Path) -> None:
        with pytest.raises(CharterError) as ei:
            await load_charter(tmp_path)
        assert str(charter_path(tmp_path)) in str(ei.value)
        assert "signed" in str(ei.value)

    async def test_valid_charter_loads(self, tmp_path: Path) -> None:
        _write_charter(tmp_path, VALID_YAML_ONLY)
        charter = await load_charter(tmp_path)
        assert charter.definition_of_done == [
            "Every fixture in tests/fixtures/import round-trips byte-identically."
        ]


# ── charter_start_error: the git-tracked gate ──────────────────────────


class TestStartGate:
    async def test_committed_charter_starts(self, tmp_path: Path) -> None:
        repo = make_chartered_repo(tmp_path)
        assert await charter_start_error(repo) is None

    async def test_modified_after_commit_refuses(self, tmp_path: Path) -> None:
        repo = make_chartered_repo(tmp_path)
        _write_charter(repo, VALID_YAML_ONLY.replace("3.0", "99.0"))

        reason = await charter_start_error(repo)

        assert reason is not None
        assert "uncommitted" in reason

    async def test_staged_but_uncommitted_refuses(self, tmp_path: Path) -> None:
        repo = make_chartered_repo(tmp_path)
        _write_charter(repo, VALID_YAML_ONLY.replace("importer", "importer v2"))
        _stage_all(repo)

        reason = await charter_start_error(repo)

        assert reason is not None
        assert "uncommitted" in reason

    async def test_untracked_charter_refuses(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        _write_charter(repo, VALID_YAML_ONLY)  # written, never committed

        reason = await charter_start_error(repo)

        assert reason is not None
        assert "not git-tracked" in reason

    async def test_no_git_repo_refuses(self, tmp_path: Path) -> None:
        ws = tmp_path / "plain"
        ws.mkdir()
        _write_charter(ws, VALID_YAML_ONLY)

        reason = await charter_start_error(ws)

        assert reason is not None
        assert "not inside a git repository" in reason

    async def test_invalid_charter_refuses_with_field_name(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        _write_charter(repo, "definition_of_done: [done]\n")  # no objective
        _commit_all(repo, "commit an invalid charter")

        reason = await charter_start_error(repo)

        assert reason is not None
        assert "objective" in reason

    async def test_missing_charter_refuses(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        reason = await charter_start_error(repo)
        assert reason is not None
        assert CHARTER_RELATIVE_PARTS[-1] in reason

    async def test_ambient_git_dir_cannot_bless_this_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GIT_DIR pointing at a repo where an identical charter happens
        # to be committed must not make this workspace's untracked
        # charter look clean — repo discovery is cwd-based in the gate.
        (tmp_path / "other").mkdir()
        other = make_chartered_repo(tmp_path / "other")
        here = make_repo(tmp_path, name="here")
        _write_charter(here, VALID_YAML_ONLY)  # never committed here
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))

        reason = await charter_start_error(here)

        assert reason is not None
        assert "not git-tracked" in reason

    async def test_skip_worktree_bit_cannot_hide_a_tamper(self, tmp_path: Path) -> None:
        repo = make_chartered_repo(tmp_path)
        _write_charter(repo, VALID_YAML_ONLY.replace("3.0", "99.0"))
        _skip_worktree(repo, ".tst/autonomy/CHARTER.md")
        # git status now reports the path clean; the gate hashes bytes
        # against HEAD instead of trusting it.

        reason = await charter_start_error(repo)

        assert reason is not None
        assert "commit them" in reason
