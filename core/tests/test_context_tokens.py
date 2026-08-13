"""Tests for token counting and the instruction stack (TD-506).

Covers: heuristic token counting, tiktoken path (via a fake counter),
200-line soft warnings, total steering budget, and the
instruction_stack protocol event builder.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tstd.context import (
    ContextAssembler,
    HeuristicTokenCounter,
    SteeringFileResolver,
    TokenCount,
    build_instruction_stack,
    make_token_counter,
)
from tstd.context.assembler import LINE_LIMIT
from tstd.context.tokens import TiktokenTokenCounter
from tstd.protocol import (
    InstructionStack,
    InstructionStackEntry,
    parse_daemon_event,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_workspace(
    base: Path,
    *,
    root_file: str | None = None,
    rules: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    home = base / "home"
    workspace = base / "workspace"
    if root_file is not None:
        _write(workspace / "AGENTS.md", root_file)
    if rules:
        for name, content in rules.items():
            _write(workspace / ".tst" / "rules" / name, content)
    return home, workspace


def _make_assembler(home: Path, **kwargs: object) -> ContextAssembler:
    return ContextAssembler(resolver=SteeringFileResolver(home_dir=home), **kwargs)  # type: ignore[arg-type]


# ── Tests: heuristic counting ─────────────────────────────────────────────


class TestHeuristicCounting:
    """The documented 4-char approximation."""

    def test_empty_text(self) -> None:
        assert HeuristicTokenCounter().count("").count == 1  # min 1

    def test_short_text(self) -> None:
        # "hi" = 2 chars → ceil(2/4) = 1
        assert HeuristicTokenCounter().count("hi").count == 1

    def test_known_counts(self) -> None:
        counter = HeuristicTokenCounter()
        # 8 chars → 2 tokens
        assert counter.count("abcdefgh").count == 2
        # 9 chars → 3 tokens (ceil)
        assert counter.count("abcdefghi").count == 3

    def test_method_is_documented(self) -> None:
        count = HeuristicTokenCounter().count("hello")
        assert count.method == "approximation (4 chars/token)"


# ── Tests: tiktoken counter ───────────────────────────────────────────────


class _FakeEncoding:
    """Stand-in for a tiktoken encoding with a known token count."""

    name = "fake_encoding"

    def __init__(self, tokens: int) -> None:
        self._tokens = tokens

    def encode(self, text: str) -> list[int]:
        return [0] * self._tokens


class TestTiktokenCounter:
    """The tiktoken path — via a fake to avoid the real dependency."""

    def test_unknown_model_falls_back_to_heuristic(self) -> None:
        """make_token_counter for an unknown slug uses the heuristic."""
        counter = TiktokenTokenCounter("gpt-4-unknown-model-xyz")
        count = counter.count("hello world")
        assert count.method == "approximation (4 chars/token)"

    def test_no_tiktoken_installed_falls_back(self) -> None:
        counter = TiktokenTokenCounter("gpt-4o")
        # If tiktoken isn't installed, it must fall back gracefully.
        assert counter.count("hello").count >= 1

    def test_encoding_used_when_available(self) -> None:
        counter = TiktokenTokenCounter("fake-model")
        counter._encoding = _FakeEncoding(tokens=42)
        count = counter.count("anything")
        assert count.count == 42
        assert count.method == "tiktoken:fake_encoding"

    def test_make_token_counter_none_returns_heuristic(self) -> None:
        assert isinstance(make_token_counter(None), HeuristicTokenCounter)

    def test_make_token_counter_slug_returns_tiktoken_counter(self) -> None:
        assert isinstance(make_token_counter("some-model"), TiktokenTokenCounter)


# ── Tests: assembler token counts ─────────────────────────────────────────


class TestAssemblerTokenCounts:
    """Per-source counts and the total budget."""

    def test_per_source_token_count(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="Rule one.\nRule two.\n")
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.sources) == 1
        # "Rule one.\nRule two.\n" = 19 chars → ceil(19/4) = 5
        assert result.sources[0].token_count.count == 5
        assert result.sources[0].token_count.method == "approximation (4 chars/token)"

    def test_total_tokens_sums_active(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root rule.\n",
            rules={"a.md": "Alpha rule.\n", "b.md": "Beta rule.\n"},
        )
        result = _make_assembler(home).assemble_sync(ws)
        expected = sum(s.token_count.count for s in result.sources if s.active)
        assert result.total_tokens.count == expected
        assert result.total_tokens.method == "approximation (4 chars/token)"

    def test_total_tokens_excludes_inactive(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n",
            rules={"scoped.md": "---\nappliesTo: [src/**]\n---\nScoped rules here.\n"},
        )
        result = _make_assembler(home).assemble_sync(ws, matched_paths={"other/file.py"})
        # The scoped rule is inactive — its tokens don't count toward the budget
        inactive = [s for s in result.sources if not s.active]
        assert len(inactive) == 1
        assert result.total_tokens.count == sum(
            s.token_count.count for s in result.sources if s.active
        )
        # Inactive source still carries its count (inspector shows it)
        assert inactive[0].token_count.count > 0

    def test_empty_workspace_total_zero(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path)
        result = _make_assembler(home).assemble_sync(ws)
        assert result.total_tokens.count == 0


# ── Tests: 200-line warning ───────────────────────────────────────────────


class TestLineLimitWarning:
    """Files over 200 lines get a soft warning."""

    def test_no_warning_below_limit(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="\n".join(f"line {i}" for i in range(50)))
        result = _make_assembler(home).assemble_sync(ws)
        assert result.sources[0].warnings == ()

    def test_warning_at_exactly_limit(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path, root_file="\n".join(f"line {i}" for i in range(LINE_LIMIT))
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert result.sources[0].warnings == ()

    def test_warning_over_limit(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="\n".join(f"line {i}" for i in range(LINE_LIMIT + 1)),
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.sources[0].warnings) == 1
        warning = result.sources[0].warnings[0]
        assert "exceeds" in warning
        assert "adherence" in warning  # cites reduced adherence
        assert "authoring guide" in warning  # links the guide

    def test_warning_per_source(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="\n".join(f"line {i}" for i in range(LINE_LIMIT + 1)),
            rules={"short.md": "Short.\n"},
        )
        result = _make_assembler(home).assemble_sync(ws)
        # Workspace file (over limit) warns; rule file (short) does not
        long_sources = [s for s in result.sources if s.path.name == "AGENTS.md"]
        short_sources = [s for s in result.sources if s.path.name == "short.md"]
        assert len(long_sources[0].warnings) == 1
        assert short_sources[0].warnings == ()


# ── Tests: custom token counter injection ─────────────────────────────────


class _FixedCounter:
    """A deterministic token counter for injection tests."""

    def count(self, text: str) -> TokenCount:
        return TokenCount(count=7, method="fixed:test")


class TestCounterInjection:
    """The assembler accepts an injected token counter."""

    def test_injected_counter_used(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="Some content.\n")
        assembler = ContextAssembler(
            resolver=SteeringFileResolver(home_dir=home),
            token_counter=_FixedCounter(),
        )
        result = assembler.assemble_sync(ws)
        assert result.sources[0].token_count.count == 7
        assert result.sources[0].token_count.method == "fixed:test"
        assert result.total_tokens.count == 7
        assert result.total_tokens.method == "fixed:test"


# ── Tests: instruction stack builder ──────────────────────────────────────


class TestInstructionStack:
    """The instruction_stack protocol event carries counts."""

    def test_build_instruction_stack(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n",
            rules={"style.md": "Style rules.\n"},
        )
        steering = _make_assembler(home).assemble_sync(ws)
        event = build_instruction_stack("sess-1", steering, seq=3)

        assert isinstance(event, InstructionStack)
        assert event.seq == 3
        assert event.session_id == "sess-1"
        assert len(event.sources) == 2
        assert event.total_tokens == steering.total_tokens.count
        assert event.token_method == steering.total_tokens.method

        # Entries carry per-source counts
        for entry, source in zip(event.sources, steering.sources, strict=True):
            assert isinstance(entry, InstructionStackEntry)
            assert entry.path == str(source.path)
            assert entry.precedence == source.precedence.label
            assert entry.active == source.active
            assert entry.tokens == source.token_count.count
            assert entry.token_method == source.token_count.method
            assert entry.warnings == list(source.warnings)

    def test_stack_includes_inactive_with_counts(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n",
            rules={"scoped.md": "---\nappliesTo: [src/**]\n---\nScoped.\n"},
        )
        steering = _make_assembler(home).assemble_sync(ws, matched_paths={"other.py"})
        event = build_instruction_stack("sess-1", steering)
        inactive = [s for s in event.sources if not s.active]
        assert len(inactive) == 1
        assert inactive[0].tokens > 0  # cost shown, not counted in total
        # Total covers active sources only
        active_tokens = sum(s.tokens for s in event.sources if s.active)
        assert event.total_tokens == active_tokens

    def test_stack_round_trips_over_the_wire(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="Root.\n")
        steering = _make_assembler(home).assemble_sync(ws)
        event = build_instruction_stack("sess-1", steering)
        # Serialize + parse through the daemon event adapter (discriminated union)
        raw = event.model_dump_json()
        parsed = parse_daemon_event(raw)
        assert isinstance(parsed, InstructionStack)
        assert parsed.session_id == "sess-1"
        assert parsed.total_tokens == event.total_tokens

    def test_stack_requires_session_id(self) -> None:
        with pytest.raises(ValidationError):
            InstructionStack(total_tokens=0, token_method="")  # type: ignore[call-arg]


# ── Tests: line_count ─────────────────────────────────────────────────────


class TestLineCount:
    def test_line_count_reported(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="a\nb\nc\n")
        result = _make_assembler(home).assemble_sync(ws)
        assert result.sources[0].line_count == 3

    def test_line_count_single_line(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="only line")
        result = _make_assembler(home).assemble_sync(ws)
        assert result.sources[0].line_count == 1
