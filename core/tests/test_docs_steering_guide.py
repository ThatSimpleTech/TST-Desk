"""The steering authoring guide is checked against the assembler (TD-1502).

A guide whose worked examples do not behave as described is worse than
none: the reader trusts it, writes steering to match, and gets a stack
they did not ask for.  So every example in ``docs/steering.md`` is a real
workspace tree, and this module builds it on disk and runs it through
``ContextAssembler`` rather than an imitation of one.

An example is written as a run of HTML comment markers:

* ``<!-- verify: example <id> -->`` starts a fresh fixture.
* ``<!-- verify: file <relpath> -->`` puts the next fenced block on disk.
  A path starting ``~/`` lands in the fixture's home directory instead of
  the workspace.
* ``<!-- verify: touched -->`` sets the workspace-relative paths the
  session has touched (one per line, ``(none)`` for the empty set).
  Without it the example assembles with ``matched_paths=None``.
* ``<!-- verify: stack -->``, ``block``, ``issues`` and ``pending`` are
  assertions, evaluated against the fixture *as it stands at that point*
  in the document, so an example can show a before and an after around a
  second ``touched`` marker.

Beyond the examples, the doc-wide tests below keep the prose itself from
rotting: the line limit, the depth limit and the warning text are read
out of the code and must appear in the guide, so moving any of them
fails the suite until the guide catches up.
"""

from __future__ import annotations

import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tstd.context.assembler import LINE_LIMIT, AssembledSteering, ContextAssembler, ResolvedSource
from tstd.context.discover import _SKIP_DIRS, Precedence, SteeringFileResolver
from tstd.context.imports import MAX_IMPORT_DEPTH
from tstd.context.tier import DEFAULT_VALIDATOR_SUBSET

ROOT = Path(__file__).resolve().parent.parent.parent
DOC = ROOT / "docs" / "steering.md"
README = ROOT / "README.md"

_MARKER = re.compile(r"^<!--\s*verify:\s*(?P<kind>[a-z]+)(?:\s+(?P<arg>\S+))?\s*-->\s*$")
_FENCE = re.compile(r"^(?P<ticks>`{3,})\w*\s*$")

_EXPECTATIONS = frozenset({"stack", "block", "issues", "pending"})
_INPUTS = frozenset({"file", "touched"})

#: Stand-ins for the two absolute paths a fixture cannot know in advance.
_WORKSPACE = "<workspace>"
_HOME = "~"

#: An expectation block containing only this asserts "nothing here".
_EMPTY = "(none)"


# ── Parsing the guide ────────────────────────────────────────────────────


class Op:
    """One marker and the fenced block it governs."""

    def __init__(self, kind: str, arg: str | None, body: str, line: int) -> None:
        self.kind = kind
        self.arg = arg
        self.body = body
        self.line = line


class Example:
    """One named fixture: its ops, in document order."""

    def __init__(self, name: str, line: int) -> None:
        self.name = name
        self.line = line
        self.ops: list[Op] = []

    def __repr__(self) -> str:  # pragma: no cover - pytest ids only
        return self.name


def _doc_text() -> str:
    assert DOC.exists(), f"{DOC} is missing"
    return DOC.read_text(encoding="utf-8")


def _parse(text: str) -> list[Example]:
    """Collect every ``verify:`` marker in the guide into its example.

    Fences of four or more backticks are honoured, so a steering file
    shown inside an example may itself contain a code block.
    """
    lines = text.split("\n")
    examples: list[Example] = []
    pending: tuple[str, str | None, int] | None = None
    index = 0
    while index < len(lines):
        marker = _MARKER.match(lines[index])
        if marker:
            kind, arg = marker.group("kind"), marker.group("arg")
            assert kind in _EXPECTATIONS | _INPUTS | {"example"}, (
                f"{DOC}:{index + 1}: unknown verify kind {kind!r}"
            )
            if kind == "example":
                assert arg, f"{DOC}:{index + 1}: `example` needs a name"
                examples.append(Example(arg, index + 1))
                pending = None
            else:
                assert examples, f"{DOC}:{index + 1}: `{kind}` before any `example`"
                pending = (kind, arg, index + 1)
            index += 1
            continue

        fence = _FENCE.match(lines[index])
        if fence and pending is not None:
            ticks = fence.group("ticks")
            close = index + 1
            while close < len(lines) and lines[close].rstrip() != ticks:
                close += 1
            assert close < len(lines), f"{DOC}:{index + 1}: unterminated block"
            kind, arg, at = pending
            examples[-1].ops.append(Op(kind, arg, "\n".join(lines[index + 1 : close]), at))
            pending = None
            index = close + 1
            continue

        if lines[index].strip():
            pending = None  # a marker only reaches the block right after it
        index += 1

    assert examples, "the guide has no verifiable examples"
    return examples


EXAMPLES = _parse(_doc_text())


# ── Running an example ───────────────────────────────────────────────────


def _write(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")


def _relative(path: Path, workspace: Path, home: Path) -> str:
    """Render an absolute path the way the guide writes it."""
    for root, prefix in ((workspace, ""), (home, "~/")):
        try:
            return prefix + path.relative_to(root).as_posix()
        except ValueError:
            continue
    return path.as_posix()  # pragma: no cover - fixtures stay inside the roots


def _render_source(source: ResolvedSource, workspace: Path, home: Path) -> str:
    """One stack line: path, scope label, and the flags the guide names."""
    label = source.precedence.label
    if source.subtree is not None:
        label = f"{label}: {source.subtree}"
    flags: list[str] = []
    if source.is_fallback:
        flags.append("claude-fallback")
    if source.shadowed_path is not None:
        flags.append(f"shadows {_relative(source.shadowed_path, workspace, home)}")
    if source.applies_to is not None:
        flags.append("scoped")
    if not source.active:
        flags.append("inactive")
    if source.warnings:
        flags.append("over-limit")
    line = f"{_relative(source.path, workspace, home)} ({label})"
    return f"{line} [{' '.join(flags)}]" if flags else line


def _normalise(text: str, workspace: Path, home: Path) -> str:
    return text.replace(workspace.as_posix(), _WORKSPACE).replace(home.as_posix(), _HOME)


def _expected_lines(body: str) -> list[str]:
    if body.strip() == _EMPTY:
        return []
    return [re.sub(r"\s+", " ", line).strip() for line in body.split("\n") if line.strip()]


def _actual(kind: str, result: AssembledSteering, workspace: Path, home: Path) -> list[str] | str:
    if kind == "stack":
        return [re.sub(r"\s+", " ", _render_source(s, workspace, home)) for s in result.sources]
    if kind == "issues":
        return [_normalise(issue, workspace, home) for issue in result.import_issues]
    if kind == "pending":
        return [_relative(p, workspace, home) for p in result.pending_imports]
    return _normalise(result.block, workspace, home).strip()


@pytest.mark.parametrize("example", EXAMPLES, ids=repr)
def test_example_behaves_as_documented(example: Example) -> None:
    """Build the example's tree and check every assertion it makes."""
    assert any(op.kind in _EXPECTATIONS for op in example.ops), (
        f"{DOC}:{example.line}: example {example.name!r} asserts nothing"
    )
    with TemporaryDirectory() as tmp:
        # Resolved up front: discovery reports the home path exactly as it
        # was handed in, so an unresolved seam here would render absolute
        # paths where the guide writes `~/`.
        root = Path(tmp).resolve()
        workspace = root / "workspace"
        home = root / "home"
        workspace.mkdir()
        home.mkdir()
        assembler = ContextAssembler(resolver=SteeringFileResolver(home_dir=home))
        touched: set[str] | None = None

        for op in example.ops:
            where = f"{DOC}:{op.line}"
            if op.kind == "file":
                assert op.arg, f"{where}: `file` needs a path"
                target = home if op.arg.startswith("~/") else workspace
                _write(target, op.arg.removeprefix("~/"), op.body)
                continue
            if op.kind == "touched":
                touched = set(_expected_lines(op.body))
                continue

            result = assembler.assemble_sync(workspace, matched_paths=touched)
            actual = _actual(op.kind, result, workspace, home)
            expected: list[str] | str = (
                _normalise(op.body, workspace, home).strip()
                if op.kind == "block"
                else _expected_lines(op.body)
            )
            assert actual == expected, f"{where}: {op.kind} does not match the assembler"


# ── The prose, checked against the code ──────────────────────────────────


def _warning_text() -> str:
    """The real over-limit warning, produced by the real assembler."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        workspace, home = root / "workspace", root / "home"
        workspace.mkdir()
        home.mkdir()
        (workspace / "AGENTS.md").write_text(
            "\n".join(f"line {i}" for i in range(LINE_LIMIT + 1)), encoding="utf-8"
        )
        result = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(
            workspace
        )
        assert result.sources[0].warnings, "the fixture should have tripped the line limit"
        return result.sources[0].warnings[0]


def test_the_line_limit_warning_is_quoted_verbatim() -> None:
    """Criterion 2: the guide states the limit in the assembler's own words.

    Quoting the produced string rather than restating it means moving
    ``LINE_LIMIT`` — or rewording the warning — fails here first.  The
    warning points the reader at "the steering authoring guide"; this is
    that guide, so the two have to agree.
    """
    assert _warning_text() in _doc_text(), (
        "the guide does not quote the assembler's over-limit warning verbatim"
    )


def test_nothing_is_dropped_past_the_line_limit() -> None:
    """The guide says a long file warns and still loads in full."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        workspace, home = root / "workspace", root / "home"
        workspace.mkdir()
        home.mkdir()
        last = f"line {LINE_LIMIT + 49}"
        (workspace / "AGENTS.md").write_text(
            "\n".join(f"line {i}" for i in range(LINE_LIMIT + 50)), encoding="utf-8"
        )
        result = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(
            workspace
        )
        assert result.sources[0].warnings, "an over-limit file should warn"
        assert last in result.block, "an over-limit file is still assembled in full"


def test_the_depth_limit_is_stated_in_the_assemblers_words() -> None:
    """The guide quotes the depth-exceeded message the reader will see."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        workspace, home = root / "workspace", root / "home"
        workspace.mkdir()
        home.mkdir()
        (workspace / "AGENTS.md").write_text("root\n@a.md\n", encoding="utf-8")
        for this, nxt in zip("abcde", "bcdef", strict=True):
            (workspace / f"{this}.md").write_text(f"{this}\n@{nxt}.md\n", encoding="utf-8")
        result = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(
            workspace
        )
        assert result.import_issues, "a five-deep chain should exceed the limit"
        message = result.import_issues[0]
        assert message.startswith(f"max import depth {MAX_IMPORT_DEPTH} exceeded")
        assert message in _doc_text(), (
            "the guide does not quote the depth-exceeded message verbatim"
        )


def test_every_precedence_level_is_documented() -> None:
    """A new scope in the hierarchy fails this until the guide names it."""
    text = _doc_text()
    missing = [level.label for level in Precedence if level.label not in text]
    assert not missing, "undocumented precedence levels: " + ", ".join(missing)


def test_every_skipped_directory_is_documented() -> None:
    """The nested walk's blind spots are the ones that surprise authors."""
    text = _doc_text()
    missing = [name for name in sorted(_SKIP_DIRS) if f"`{name}`" not in text]
    assert not missing, "undocumented skipped directories: " + ", ".join(missing)


def test_the_validator_subset_is_documented() -> None:
    """Rule names decide what the validator sees, so name the patterns."""
    text = _doc_text()
    missing = [p for p in DEFAULT_VALIDATOR_SUBSET if f"`{p}`" not in text]
    assert not missing, "undocumented validator subset patterns: " + ", ".join(missing)


def test_the_guide_is_reachable_from_the_readme() -> None:
    """The warning tells the reader to see the guide; give them the path."""
    assert "docs/steering.md" in README.read_text(encoding="utf-8"), (
        "README.md does not link the steering guide"
    )
