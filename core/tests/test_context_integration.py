"""Cross-cutting context assembler suite (TD-1403).

The per-story tests cover each feature against minimal trees. This suite
covers the *seams between the features* — precedence, fallback, glob
scoping, and imports acting on one fixture workspace at the same time,
with golden files pinning the exact assembled prompt and a determinism
property test over randomized trees.

Golden regeneration: set ``TSTD_UPDATE_GOLDEN=1`` to rewrite the golden
files from current behavior, then review the diff like any code change.
Absolute tmp paths are replaced with ``<ROOT>`` before comparison so the
goldens are machine-independent.
"""

from __future__ import annotations

import hashlib
import os
import random
from pathlib import Path

import pytest

from tests.test_loop import make_config, mock_factory, wait_for_turn
from tstd.context import (
    BASE_SYSTEM_PROMPT,
    AssembledSteering,
    ContextAssembler,
    Precedence,
    PromptAssembler,
    SteeringFileResolver,
    assemble_for_tier_sync,
)
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

_GOLDEN_DIR = Path(__file__).parent / "golden"
_ROOT = "<ROOT>"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _to_golden(text: str, base: Path) -> str:
    return text.replace(str(base), _ROOT)


def _assert_golden(name: str, actual: str) -> None:
    path = _GOLDEN_DIR / name
    if os.environ.get("TSTD_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    expected = path.read_text(encoding="utf-8") if path.exists() else ""
    assert actual == expected, (
        f"golden mismatch vs {path} — set TSTD_UPDATE_GOLDEN=1 to regenerate, then review the diff"
    )


# ── The fixture workspace ──────────────────────────────────────────────
#
# One tree that simultaneously exercises: user global (with its
# directory fallback shadowed), workspace CLAUDE.md fallback, globbed
# rules (always-on, scoped, overlapping scopes, `...` frontmatter
# closer), nested AGENTS.md + nested CLAUDE.md fallback, imports at two
# depths, imports from rule files, a depth-limit violation, an import
# cycle, a fenced @-directive that must not resolve, and frontmatter
# inside an imported file that must stay literal.


def build_fixture_workspace(base: Path) -> tuple[Path, Path]:
    home = base / "home"
    ws = base / "workspace"

    _write(home / ".tstdesk" / "AGENTS.md", "[GLOBAL-MAIN]\n")
    # Global-level fallback lives in a different directory; present here
    # only to prove it is ignored while .tstdesk/AGENTS.md exists.
    _write(home / ".claude" / "CLAUDE.md", "[GLOBAL-FALLBACK-SHADOWED]\n")

    # No workspace AGENTS.md — CLAUDE.md is the workspace-level fallback.
    _write(
        ws / "CLAUDE.md",
        "[WS-FALLBACK]\n@docs/imported.md\n@chain/a.md\n@cycle/x.md\n",
    )

    _write(
        ws / "docs" / "imported.md",
        "---\ntitle: pinned literal — imported files keep frontmatter\n---\n"
        "[IMPORTED-DOC]\n"
        "```\n@phantom.md\n```\n"  # fenced: must NOT resolve
        "@nested/deep.md\n",
    )
    _write(ws / "docs" / "nested" / "deep.md", "[IMPORTED-DEEP]\n")
    _write(ws / "docs" / "rule-import.md", "[RULE-IMPORT]\n")

    # Import depth: root -> a(1) -> b(2) -> c(3) -> d(4) -> e(5, exceeds).
    for name, target in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")):
        _write(ws / "chain" / f"{name}.md", f"[CHAIN-{name.upper()}]\n@{target}.md\n")
    _write(ws / "chain" / "e.md", "[CHAIN-E]\n")
    # Import cycle: x -> y -> x.
    _write(ws / "cycle" / "x.md", "[CYCLE-X]\n@y.md\n")
    _write(ws / "cycle" / "y.md", "[CYCLE-Y]\n@x.md\n")

    _write(ws / ".tst" / "rules" / "always.md", "[RULE-ALWAYS]\n")
    _write(
        ws / ".tst" / "rules" / "api-scoped.md",
        "---\nappliesTo:\n  - src/api/**\n---\n[RULE-API-SCOPED]\n@../../docs/rule-import.md\n",
    )
    _write(
        ws / ".tst" / "rules" / "dual-g.md",
        "---\nappliesTo:\n  - src/**\n...\n[RULE-DUAL-GLOB]\n",  # '.'-closed frontmatter
    )
    _write(ws / ".tst" / "rules" / "standards-code.md", "[RULE-STANDARDS]\n")

    _write(ws / "src" / "AGENTS.md", "[NESTED-SRC]\n")
    _write(ws / "src" / "api" / "handler.py", "def handler() -> None: ...\n")
    _write(ws / "tests" / "CLAUDE.md", "[NESTED-TESTS-FALLBACK]\n")

    return home, ws


@pytest.fixture
def fixture(tmp_path: Path) -> tuple[Path, Path]:
    return build_fixture_workspace(tmp_path)


def _assemble(home: Path, ws: Path, matched_paths: set[str] | None = None) -> AssembledSteering:
    return ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(
        ws, matched_paths=matched_paths
    )


# ── Criterion 1+2: the combined fixture against golden files ───────────


def test_full_assembly_matches_golden(fixture: tuple[Path, Path]) -> None:
    home, ws = fixture
    assembled = _assemble(home, ws)
    _assert_golden("block_all.md", _to_golden(assembled.block, ws.parent))

    # Every discovered source, in precedence order.
    labels = [s.precedence for s in assembled.sources]
    assert labels == [
        Precedence.USER_GLOBAL,
        Precedence.WORKSPACE,
        Precedence.RULES,
        Precedence.RULES,
        Precedence.RULES,
        Precedence.RULES,
        Precedence.NESTED,
        Precedence.NESTED,
    ]
    assert all(s.active for s in assembled.sources)

    # Fallback semantics inside the combined tree.
    ws_source = next(s for s in assembled.sources if s.precedence is Precedence.WORKSPACE)
    assert ws_source.is_fallback
    nested = [s for s in assembled.sources if s.precedence is Precedence.NESTED]
    assert {str(s.subtree) for s in nested} == {"src", "tests"}
    assert not next(s for s in nested if s.subtree == "src").is_fallback
    assert next(s for s in nested if s.subtree == "tests").is_fallback

    # Shadowed global fallback never surfaces.
    assert "[GLOBAL-FALLBACK-SHADOWED]" not in assembled.block

    # Fence-wrapped directive passed through unexpanded; frontmatter in
    # the imported file stayed literal; depth and cycle issues recorded.
    assert "@phantom.md" in assembled.block
    assert "title: pinned literal" in assembled.block
    assert any("max import depth 4 exceeded" in i for i in assembled.import_issues)
    assert any("import cycle detected" in i for i in assembled.import_issues)


def test_scoped_rules_gate_on_matched_paths(fixture: tuple[Path, Path]) -> None:
    home, ws = fixture

    # A path matching no glob: both scoped rules become inactive and their
    # imports are not processed — everything else is unchanged.
    gated = _assemble(home, ws, matched_paths={"README.md"})
    _assert_golden("block_readme.md", _to_golden(gated.block, ws.parent))
    rules = {s.path.name: s for s in gated.sources if s.precedence is Precedence.RULES}
    assert not rules["api-scoped.md"].active
    assert not rules["dual-g.md"].active
    assert rules["api-scoped.md"].imports == ()  # inactive: imports not processed
    assert "[RULE-IMPORT]" not in gated.block
    assert "[RULE-ALWAYS]" in gated.block

    # A path matching both overlapping globs reactivates them; the block
    # equals the unfiltered assembly.
    matched = _assemble(home, ws, matched_paths={"src/api/handler.py"})
    assert matched.block == _assemble(home, ws).block


def test_validator_tier_subset_matches_golden(fixture: tuple[Path, Path]) -> None:
    home, ws = fixture
    context = assemble_for_tier_sync("validator", workspace_path=ws, home_dir=home)
    _assert_golden("block_validator.md", _to_golden(context.blocks["steering"], ws.parent))

    # The subset keeps bare AGENTS.md (which matches at any depth) and the
    # standards rule; fallbacks and non-standards rules are filtered out
    # entirely (not merely deactivated).
    names = {s.path.name for s in context.steering.sources}
    assert names == {"AGENTS.md", "standards-code.md"}

    # Brain and worker see the full set — tier routing is the only filter.
    full = assemble_for_tier_sync("brain", workspace_path=ws, home_dir=home)
    assert len(full.steering.sources) == 8


# ── Criterion 3: determinism property test ─────────────────────────────

_MARKERS = ["alpha", "beta", "gamma", "delta", "epsilon"]
_RULE_NAMES = ["one", "two", "three"]


def _build_random_workspace(base: Path, rng: random.Random) -> tuple[Path, Path]:
    """Randomized tree over the feature space the suite pins."""
    home = base / "home"
    ws = base / "workspace"

    if rng.random() < 0.7:
        _write(home / ".tstdesk" / "AGENTS.md", f"global {rng.choice(_MARKERS)}\n")
    elif rng.random() < 0.5:
        _write(home / ".claude" / "CLAUDE.md", f"global fallback {rng.choice(_MARKERS)}\n")

    if rng.random() < 0.6:
        _write(ws / "AGENTS.md", f"root agents {rng.choice(_MARKERS)}\n")
    else:
        body = f"root claude {rng.choice(_MARKERS)}\n"
        if rng.random() < 0.5:
            body += "@docs/ref.md\n"
        _write(ws / "CLAUDE.md", body)
        _write(ws / "docs" / "ref.md", f"imported {rng.choice(_MARKERS)}\n")

    rng.shuffle(names := list(_RULE_NAMES))
    for name in names[: rng.randrange(0, 3)]:
        marker = rng.choice(_MARKERS)
        if rng.random() < 0.5:
            glob = rng.choice(["src/**", "**/*.py", "docs/**"])
            body = f"---\nappliesTo:\n  - {glob}\n---\nrule {name} {marker}\n"
        else:
            body = f"rule {name} {marker}\n"
        if rng.random() < 0.4:
            body += "@../../docs/ref.md\n"
            _write(ws / "docs" / "ref.md", f"imported {rng.choice(_MARKERS)}\n")
        _write(ws / ".tst" / "rules" / f"{name}.md", body)

    for sub in rng.sample(["src", "lib"], k=rng.randrange(0, 3)):
        _write(ws / sub / "AGENTS.md", f"nested {sub} {rng.choice(_MARKERS)}\n")

    return home, ws


@pytest.mark.parametrize("seed", [20260813, 20260814, 20260815, 20260816])
def test_assembly_is_deterministic(tmp_path: Path, seed: int) -> None:
    """Identical inputs assemble to identical bytes — twice, and across a
    fresh resolver instance (no hidden cache or iteration-order drift)."""
    home, ws = _build_random_workspace(tmp_path, random.Random(seed))
    matched_paths = {"src/x.py", "README.md"}

    first = _assemble(home, ws, matched_paths=matched_paths)
    second = _assemble(home, ws, matched_paths=matched_paths)
    fresh = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(
        ws, matched_paths=matched_paths
    )

    assert first.block == second.block == fresh.block
    assert first.import_issues == second.import_issues == fresh.import_issues
    assert [s.path for s in first.sources] == [s.path for s in fresh.sources]
    assert first.total_tokens == second.total_tokens


# ── Criterion 4: cache-prefix stability across turns ───────────────────


async def test_cache_prefix_stable_across_turns(fixture: tuple[Path, Path], tmp_path: Path) -> None:
    """A real multi-turn loop over the combined fixture: the cache prefix
    hash is byte-stable across turns and identical to what the assembly
    library computes directly — the loop and the library agree."""
    home, ws = fixture
    assembler = PromptAssembler(ws, home_dir=home)
    session = Session(str(ws))
    mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Hi")})
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(),
            mock_factory(mock),
            make_config(),
            prompt_assembler=assembler,
        ),
    )
    await runner.start()
    hashes: list[str] = []
    for turn, message in enumerate(["one", "two", "three"], start=1):
        await session.add_user_message(message)
        await wait_for_turn(session, turn)
        assert assembler.last_assembled is not None
        hashes.append(assembler.last_assembled.prefix_hash)
    await runner.cancel()

    assert len(set(hashes)) == 1
    # The library path computes the same hash over the same fixture.
    direct = assembler.assemble_sync("brain")
    assert hashes[0] == direct.prefix_hash
    # And the hash really is sha256(BASE_SYSTEM_PROMPT + steering block).
    expect = hashlib.sha256(
        (BASE_SYSTEM_PROMPT + "\n\n" + _assemble(home, ws).block).encode("utf-8")
    ).hexdigest()
    assert hashes[0] == expect
    # Every turn's system message carried the stable prefix first.
    for call in mock.calls:
        first = call.messages[0].content or ""
        assert first.startswith(BASE_SYSTEM_PROMPT + "\n\n<!-- from: ")


async def test_worker_task_and_tier_do_not_touch_the_prefix(
    fixture: tuple[Path, Path],
) -> None:
    """Worker's task block and tier selection sit outside the cache prefix
    by construction — pinning that contract."""
    home, ws = fixture
    assembler = PromptAssembler(ws, home_dir=home)
    brain = assembler.assemble_sync("brain")
    worker = assembler.assemble_sync("worker", task="refactor src/api/handler.py")
    assert brain.prefix_hash == worker.prefix_hash
    assert "refactor src/api/handler.py" in worker.text
    assert "refactor src/api/handler.py" not in worker.prefix


# ── Cross-cutting seam: imports of discovered sources ──────────────────


def test_importing_a_steering_source_duplicates_it(tmp_path: Path) -> None:
    """A root file @-importing a nested steering source yields the content
    twice — once as the nested source, once imported — with distinct
    provenance. Pins behavior so a future dedupe must be deliberate."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    _write(ws / "AGENTS.md", "root\n@src/AGENTS.md\n")
    _write(ws / "src" / "AGENTS.md", "NESTED-BODY\n")

    assembled = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(ws)

    assert assembled.block.count("NESTED-BODY") == 2
    assert "(nested: src) -->\nNESTED-BODY" in assembled.block
    assert "(imported) -->\nNESTED-BODY" in assembled.block


def test_unclosed_fence_swallows_directives_to_eof(tmp_path: Path) -> None:
    """A code fence left open in an imported file protects every following
    @-directive from resolution — to end of file."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    _write(ws / "AGENTS.md", "root\n@leak.md\n")
    _write(ws / "leak.md", "before\n```\n@never.md\nafter\n")
    _write(ws / "never.md", "NEVER-RESOLVED\n")

    assembled = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(ws)

    assert "@never.md" in assembled.block  # passed through literally
    assert "NEVER-RESOLVED" not in assembled.block
    assert not any("never.md" in i for i in assembled.import_issues)
