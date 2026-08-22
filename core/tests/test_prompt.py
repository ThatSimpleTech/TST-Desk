"""Tests for cache-aware prompt assembly (TD-305, TD-1810).

Covers:
- Stable-prefix order: base → workspace root → steering → memory → manifest
- Prefix byte-identity across calls with unchanged source files
- Prefix change when steering sources change
- Worker: no manifest, no memory placeholder
- Validator: subset only + diff + test output
- Memory placeholder for brain when nothing loaded; loaded bytes replace it
  (TD-2501). Worker and validator never carry the memory slot.
- Async entry point and prefix token count
- The absolute workspace root, stated once, inside the cache prefix, with
  the manifest's relative listing left intact (TD-1810)
- Block [1b] claims nothing that is false on a tier without a manifest
- A root that cannot be stated verbatim is refused, not truncated
- Steering is reported as steering, separately from the cache prefix
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tstd.context import (
    BASE_SYSTEM_PROMPT,
    MEMORY_PLACEHOLDER,
    WORKSPACE_ROOT_LABEL,
    PromptAssembler,
    WorkspaceManifest,
    workspace_root_block,
)
from tstd.context.memory_loader import load_memory_for_task
from tstd.context.prompt import AssembledPrompt
from tstd.context.tokens import heuristic_count
from tstd.memory_store import memory_dir

# ── Fixture helpers ──────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_workspace(
    base: Path,
    *,
    global_file: str | None = None,
    root_file: str | None = None,
    rules: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Build a workspace; return ``(home_dir, workspace)``."""
    home = base / "home"
    ws = base / "workspace"
    if global_file is not None:
        _write(home / ".tstdesk" / "AGENTS.md", global_file)
    if root_file is not None:
        _write(ws / "AGENTS.md", root_file)
    if rules:
        for name, content in rules.items():
            _write(ws / ".tst" / "rules" / name, content)
    return home, ws


# ── Stable-prefix order ──────────────────────────────────────────────────


class TestStablePrefixOrder:
    def test_brain_order(self, tmp_path: Path) -> None:
        """Brain prompt: base → steering → memory placeholder → manifest."""
        home, ws = _build_workspace(tmp_path, root_file="root: use python3")
        _write(ws / "src" / "main.py", "print('hi')")
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync("brain")
        assert result.text.startswith(BASE_SYSTEM_PROMPT)
        # Base comes before steering
        base_idx = result.text.index(BASE_SYSTEM_PROMPT)
        steering_idx = result.text.index("root: use python3")
        assert base_idx < steering_idx
        # Memory placeholder comes after steering
        memory_idx = result.text.index(MEMORY_PLACEHOLDER)
        assert steering_idx < memory_idx
        # Manifest comes after memory
        manifest_idx = result.text.index("src/main.py")
        assert memory_idx < manifest_idx

    def test_worker_order(self, tmp_path: Path) -> None:
        """Worker prompt: base → steering → task."""
        home, ws = _build_workspace(tmp_path, root_file="root: be async")
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync("worker", task="Fix the bug")
        base_idx = result.text.index(BASE_SYSTEM_PROMPT)
        steering_idx = result.text.index("root: be async")
        task_idx = result.text.index("Fix the bug")
        assert base_idx < steering_idx < task_idx
        # No manifest, no memory placeholder
        assert MEMORY_PLACEHOLDER not in result.text
        assert "src/main.py" not in result.text

    def test_validator_order(self, tmp_path: Path) -> None:
        """Validator prompt: base → steering subset → diff → test output."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="# Standards",
            rules={"standards.md": "Standard: lint."},
        )
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync(
            "validator",
            diff="+new code",
            test_output="3 passed",
        )
        base_idx = result.text.index(BASE_SYSTEM_PROMPT)
        steering_idx = result.text.index("Standard: lint")
        diff_idx = result.text.index("+new code")
        test_idx = result.text.index("3 passed")
        assert base_idx < steering_idx < diff_idx < test_idx
        # No manifest, no memory, no task
        assert MEMORY_PLACEHOLDER not in result.text
        assert "Fix the bug" not in result.text

    def test_memory_placeholder_included(self, tmp_path: Path) -> None:
        """Brain with no memory includes the placeholder."""
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync("brain")
        assert MEMORY_PLACEHOLDER in result.text


class TestMemorySlot:
    """TD-2501: loaded bytes replace the placeholder; other tiers have no slot."""

    def test_loaded_bytes_replace_placeholder(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        loaded = "<!-- memory: .tst/memory/MEMORY.md (always-index) -->\ndurable: ruff\n"
        result = assembler.assemble_sync("brain", memory=loaded)
        assert MEMORY_PLACEHOLDER not in result.text
        assert "durable: ruff" in result.text
        assert result.text.index("root: steering") < result.text.index("durable: ruff")

    def test_empty_load_keeps_placeholder(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        none = assembler.assemble_sync("brain", memory=None)
        omitted = assembler.assemble_sync("brain")
        assert MEMORY_PLACEHOLDER in none.text
        assert none.prefix_hash == omitted.prefix_hash
        assert none.prefix == omitted.prefix

    def test_worker_and_validator_have_no_memory_slot(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        loaded = "<!-- memory: .tst/memory/MEMORY.md (always-index) -->\ndurable: ruff\n"
        worker = assembler.assemble_sync("worker", task="fix auth", memory=loaded)
        validator = assembler.assemble_sync("validator", diff="+x", memory=loaded)
        assert MEMORY_PLACEHOLDER not in worker.text
        assert MEMORY_PLACEHOLDER not in validator.text
        assert "durable: ruff" not in worker.text
        assert "durable: ruff" not in validator.text

    def test_swapping_memory_md_keeps_prefix_hash(self, tmp_path: Path) -> None:
        """TD-2503: memory sits after the cache prefix."""
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        mem = memory_dir(ws)
        _write(mem / "MEMORY.md", "alpha-memory\n")
        assembler = PromptAssembler(ws, home_dir=home)
        first = assembler.assemble_sync("brain", memory=load_memory_for_task(ws, "x").block)
        _write(mem / "MEMORY.md", "beta-memory\n")
        second = assembler.assemble_sync("brain", memory=load_memory_for_task(ws, "x").block)
        assert first.prefix_hash == second.prefix_hash
        assert first.prefix == second.prefix
        assert first.text != second.text
        assert "alpha-memory" in first.text
        assert "beta-memory" in second.text
        assert "alpha-memory" not in second.text


# ── Prefix stability ────────────────────────────────────────────────────


class TestPrefixStability:
    def test_prefix_identical_across_calls(self, tmp_path: Path) -> None:
        """Blocks 1-2 are byte-identical across calls with unchanged files."""
        home, ws = _build_workspace(tmp_path, root_file="root: conventions")
        assembler = PromptAssembler(ws, home_dir=home)
        a1 = assembler.assemble_sync("brain")
        a2 = assembler.assemble_sync("brain")
        assert a1.prefix_hash == a2.prefix_hash
        assert a1.prefix == a2.prefix

    def test_prefix_changes_when_steering_changes(self, tmp_path: Path) -> None:
        """Prefix hash changes when a steering file is modified."""
        home, ws = _build_workspace(tmp_path, root_file="root: original")
        assembler = PromptAssembler(ws, home_dir=home)
        original = assembler.assemble_sync("brain")

        # Modify the root steering file
        _write(ws / "AGENTS.md", "root: modified")
        changed = assembler.assemble_sync("brain")

        assert original.prefix_hash != changed.prefix_hash

    def test_prefix_stable_across_tiers(self, tmp_path: Path) -> None:
        """Blocks 1-2 are the same for brain and worker (same steering)."""
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        brain = assembler.assemble_sync("brain")
        worker = assembler.assemble_sync("worker")
        # Same steering → same prefix
        assert brain.prefix_hash == worker.prefix_hash
        # But the full text differs (worker has no manifest)
        assert brain.text != worker.text

    def test_prefix_tokens_positive(self, tmp_path: Path) -> None:
        """Prefix token count is greater than zero."""
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync("brain")
        assert result.prefix_tokens > 0


# ── Async entry point ────────────────────────────────────────────────────


class TestAsyncEntry:
    async def test_async_assemble(self, tmp_path: Path) -> None:
        """Async entry point works and returns AssembledPrompt."""
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        result = await assembler.assemble("brain")
        assert isinstance(result, AssembledPrompt)
        assert result.prefix_hash
        assert result.text
        assert "root: steering" in result.text


# ── Workspace root (TD-1810) ─────────────────────────────────────────────


def _root_from_prompt(text: str) -> str:
    """Recover the workspace root using only the prompt, as a reader would.

    Deliberately naive — one labelled line, read off the front — because
    that is the whole claim being tested: a reader given nothing but the
    prompt can find the root without inference.
    """
    for line in text.splitlines():
        if line.startswith(WORKSPACE_ROOT_LABEL):
            return line[len(WORKSPACE_ROOT_LABEL) :].strip()
    raise AssertionError(f"no {WORKSPACE_ROOT_LABEL!r} line in the prompt")


def _manifest_entries_from_prompt(text: str) -> list[str]:
    """Recover the manifest's listed paths using only the prompt."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Workspace files ("):
            return [entry for entry in lines[i + 1 :] if entry and not entry.startswith("<!--")]
    raise AssertionError("no workspace manifest header in the prompt")


def _sample_workspace(base: Path) -> tuple[Path, Path]:
    """A workspace with steering and a couple of nested files."""
    home, ws = _build_workspace(base, root_file="root: use python3")
    _write(ws / "README.md", "# readme")
    _write(ws / "src" / "app.py", "print('hi')")
    _write(ws / "src" / "deep" / "nested.py", "x = 1")
    return home, ws


class TestWorkspaceRoot:
    def test_root_stated_though_nothing_asked_for_it(self, tmp_path: Path) -> None:
        """The root is in the prompt with no task and no caller hint.

        Nothing in this call names the workspace path: the assembler is
        given a tier and nothing else, which is the state the loop is in
        when the user's message never mentions where they are.
        """
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert _root_from_prompt(result.text) == ws.resolve().as_posix()

    def test_root_stated_once(self, tmp_path: Path) -> None:
        """Exactly one labelled statement carrying the real root.

        AC-1 is "states the workspace's absolute root, once", which is two
        claims: the statement is there and carries the right path (fails at
        zero, and at a label with the wrong or an empty path after it), and
        there is only one of it (fails at two).  Counting the bare label
        tests neither — ``Workspace root:`` followed by nothing counts as
        one.  So the assertion is over whole statements: the label and the
        resolved root, on one line.
        """
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        statement = f"{WORKSPACE_ROOT_LABEL} {ws.resolve().as_posix()}"
        statements = [line for line in result.text.splitlines() if line.strip() == statement]
        assert len(statements) == 1, statements

    def test_exactly_once_counter_discriminates_one_from_two(self, tmp_path: Path) -> None:
        """The exactly-once assertion has teeth: a duplicated block trips it.

        A "once" assertion is only worth its name if it can reach two.
        Emitting the block a second time is the regression the criterion
        guards against, so the counter is run against a prompt that has it.
        """
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        statement = f"{WORKSPACE_ROOT_LABEL} {ws.resolve().as_posix()}"
        doubled = result.text + "\n\n" + workspace_root_block(ws)
        found = [line for line in doubled.splitlines() if line.strip() == statement]
        assert len(found) == 2, found

    def test_worked_example_is_not_a_second_statement(self, tmp_path: Path) -> None:
        """The root appears twice; only one occurrence is a statement.

        The block spells the join out with the real root, so the path's
        bytes occur again inside the example.  That is deliberate — it is
        what a reader joins against — and the exactly-once claim is about
        labelled statements, not about the substring.
        """
        _, ws = _sample_workspace(tmp_path)
        block = workspace_root_block(ws)
        root = ws.resolve().as_posix()
        assert block.count(root) == 2, block
        assert block.count(WORKSPACE_ROOT_LABEL) == 1, block

    @pytest.mark.parametrize("tier", ["brain", "worker", "validator"])
    def test_every_tier_is_told_the_root(self, tmp_path: Path, tier: str) -> None:
        """The worker calls the same fs_* tools, so it needs the root too."""
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync(
            tier,  # type: ignore[arg-type]
            task="Fix the bug",
            diff="+new code",
        )
        statement = f"{WORKSPACE_ROOT_LABEL} {ws.resolve().as_posix()}"
        assert _root_from_prompt(result.text) == ws.resolve().as_posix()
        assert [line for line in result.text.splitlines() if line.strip() == statement] == [
            statement
        ]

    def test_absolute_path_derivable_for_every_manifest_entry(self, tmp_path: Path) -> None:
        """Prompt-only join: every listed file resolves to a real file.

        This is the acceptance criterion in executable form — root and
        entries are both read back out of the assembled text, joined the
        way the prompt says to join them, and checked against the disk.
        No guessing step is available to the reader.
        """
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")

        root = _root_from_prompt(result.text)
        entries = _manifest_entries_from_prompt(result.text)
        assert "src/app.py" in entries, entries

        for entry in entries:
            derived = Path(f"{root}/{entry}")
            assert derived.is_absolute(), derived
            assert derived.is_file(), f"{derived} does not exist"

    def test_manifest_listing_is_unchanged(self, tmp_path: Path) -> None:
        """The manifest block is embedded verbatim — entries stay relative."""
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")

        manifest = WorkspaceManifest().build(ws).text
        assert manifest in result.text
        assert "src/app.py" in _manifest_entries_from_prompt(result.text)
        # No entry was rewritten to an absolute path.
        for entry in _manifest_entries_from_prompt(result.text):
            assert not entry.startswith("/"), entry

    def test_root_is_inside_the_cache_prefix(self, tmp_path: Path) -> None:
        """The root is session-constant, so it belongs in blocks 1-2."""
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert WORKSPACE_ROOT_LABEL in result.prefix
        assert ws.resolve().as_posix() in result.prefix

    def test_prefix_still_byte_identical_across_calls(self, tmp_path: Path) -> None:
        """Adding the root does not cost the TD-305 stability guarantee."""
        home, ws = _sample_workspace(tmp_path)
        assembler = PromptAssembler(ws, home_dir=home)
        first = assembler.assemble_sync("brain")
        second = assembler.assemble_sync("brain")
        assert first.prefix == second.prefix
        assert first.prefix_hash == second.prefix_hash

    def test_root_precedes_steering_and_the_volatile_blocks(self, tmp_path: Path) -> None:
        """Ordering: base → root → steering → memory → manifest."""
        home, ws = _sample_workspace(tmp_path)
        text = PromptAssembler(ws, home_dir=home).assemble_sync("brain").text
        base_idx = text.index(BASE_SYSTEM_PROMPT)
        root_idx = text.index(WORKSPACE_ROOT_LABEL)
        steering_idx = text.index("root: use python3")
        memory_idx = text.index(MEMORY_PLACEHOLDER)
        manifest_idx = text.index("Workspace files (")
        assert base_idx < root_idx < steering_idx < memory_idx < manifest_idx

    def test_stated_root_is_canonical(self, tmp_path: Path) -> None:
        """A symlinked workspace is stated as the path the guard accepts.

        The path guard canonicalises before comparing, so stating the
        symlink would hand the model a prefix that only accidentally
        matches the boundary it is checked against.
        """
        home, ws = _sample_workspace(tmp_path)
        link = tmp_path / "via-link"
        try:
            os.symlink(ws, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not available on this platform")

        result = PromptAssembler(link, home_dir=home).assemble_sync("brain")
        assert _root_from_prompt(result.text) == ws.resolve().as_posix()


# ── What block [1b] claims is true on every tier (TD-1810) ───────────────


class TestRootBlockClaimsHoldOnEveryTier:
    """The block goes to all three tiers verbatim, so it may not describe
    blocks only one tier receives.  Only brain is given the manifest."""

    def test_only_brain_receives_a_file_listing(self, tmp_path: Path) -> None:
        """The premise: worker and validator get no manifest at all."""
        home, ws = _sample_workspace(tmp_path)
        assembler = PromptAssembler(ws, home_dir=home)
        manifest_header = "Workspace files ("
        assert manifest_header in assembler.assemble_sync("brain").text
        assert manifest_header not in assembler.assemble_sync("worker", task="Fix it").text
        assert manifest_header not in assembler.assemble_sync("validator", diff="+x").text

    @pytest.mark.parametrize("tier", ["brain", "worker", "validator"])
    def test_block_does_not_claim_a_listing_exists(self, tmp_path: Path, tier: str) -> None:
        """No sentence asserts that workspace files *are listed*.

        A tier without a manifest would be reading a false statement, and
        a model that believes a listing exists has a reason to act as if
        it saw one.  The rule the block states — how a relative path
        resolves — is true whether or not anything was listed.
        """
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync(
            tier,  # type: ignore[arg-type]
            task="Fix the bug",
            diff="+new code",
        )
        block = workspace_root_block(ws)
        assert block in result.text
        assert "are listed" not in block
        assert "files are listed" not in block

    def test_block_states_the_resolution_rule(self, tmp_path: Path) -> None:
        """What survives the rewording: the join, with a worked example."""
        _, ws = _sample_workspace(tmp_path)
        block = workspace_root_block(ws)
        root = ws.resolve().as_posix()
        assert f'"{root}/src/app.py"' in block
        assert "joined to it" in block

    def test_block_is_byte_identical_across_tiers(self, tmp_path: Path) -> None:
        """One shared head for all three tiers — the reason for one wording."""
        home, ws = _sample_workspace(tmp_path)
        assembler = PromptAssembler(ws, home_dir=home)
        blocks = {
            assembler.assemble_sync(tier).prefix.split("\n\n")[1]
            for tier in ("brain", "worker", "validator")
        }
        assert len(blocks) == 1, blocks


# ── A root that cannot be stated verbatim (TD-1810) ──────────────────────


class TestRootWithControlCharacters:
    """A newline in the path truncates the stated root at the label line
    and turns the tail into prose — a total, silent failure of the block.
    Refused instead, loudly, at the point of rendering."""

    @pytest.mark.parametrize(
        ("name", "codepoint"),
        [
            ("newline", "\n"),
            ("carriage return", "\r"),
            ("tab", "\t"),
            ("vertical tab", "\x0b"),
            ("escape", "\x1b"),
            ("delete", "\x7f"),
            # C1 is not decorative here: U+0085 NEL is a mandatory line break
            # under UAX-14, so it truncates the stated root exactly as \n does.
            ("next line", "\u0085"),
            ("c1 low", "\u0080"),
            ("c1 high", "\u009f"),
            ("line separator", "\u2028"),
            ("paragraph separator", "\u2029"),
        ],
    )
    def test_control_character_in_root_is_refused(self, name: str, codepoint: str) -> None:
        """Every rejected class names itself in the error, not just newline."""
        with pytest.raises(ValueError, match="control characters"):
            workspace_root_block(f"/tmp/ws{codepoint}name")

    def test_error_names_the_offending_codepoint(self) -> None:
        """Loud means actionable: the message says which character and why."""
        with pytest.raises(ValueError) as excinfo:
            workspace_root_block("/tmp/ws\nname")
        message = str(excinfo.value)
        assert "U+000A" in message
        assert "rename or relocate" in message

    def test_nul_is_refused_before_the_guard_sees_it(self) -> None:
        """``pathlib`` rejects an embedded NUL, so the root never renders.

        Pinned separately because the refusal comes from a different
        layer: the guard's message is not the one raised here, and a
        future change that stopped resolving the path would silently
        hand a NUL to the prompt.
        """
        with pytest.raises(ValueError):
            workspace_root_block("/tmp/ws\x00name")

    def test_ordinary_root_still_renders(self, tmp_path: Path) -> None:
        """The guard rejects control characters, not spaces or unicode."""
        ws = tmp_path / "my workspace" / "pläts"
        ws.mkdir(parents=True)
        block = workspace_root_block(ws)
        assert f"{WORKSPACE_ROOT_LABEL} {ws.resolve().as_posix()}" in block

    def test_assembly_refuses_rather_than_stating_a_truncated_root(self, tmp_path: Path) -> None:
        """End to end on a real directory whose name contains a newline.

        Without the guard this assembles fine and ships a prompt whose
        stated root is the text before the newline — a path that exists
        often enough to be joined against and acted on.
        """
        home = tmp_path / "home"
        home.mkdir()
        ws = tmp_path / "work\nspace"
        try:
            ws.mkdir()
        except OSError:  # filesystem refuses the name — the guard is moot
            pytest.skip("filesystem rejects newlines in directory names")
        _write(ws / "AGENTS.md", "root: steering")

        with pytest.raises(ValueError, match="control characters"):
            PromptAssembler(ws, home_dir=home).assemble_sync("brain")


# ── Steering is billed as steering (TD-1810 / TD-1811 sibling) ───────────


class TestSteeringTokensAreNotPrefixTokens:
    """``prefix_tokens`` counts the base prompt and block [1b] as well as
    steering.  Reported alone under a "steering reloaded" heading it
    overstates what the user's files cost."""

    def test_steering_tokens_count_the_steering_block_alone(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="root: use python3")
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        steering_block = result.steering.block
        assert result.steering_tokens == heuristic_count(steering_block).count

    def test_prefix_tokens_exceed_steering_tokens_by_the_machinery(self, tmp_path: Path) -> None:
        """The gap is the base prompt plus block [1b], not rounding."""
        home, ws = _build_workspace(tmp_path, root_file="root: use python3")
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        machinery = (
            heuristic_count(BASE_SYSTEM_PROMPT).count
            + heuristic_count(workspace_root_block(ws)).count
        )
        assert result.prefix_tokens > result.steering_tokens
        assert result.prefix_tokens - result.steering_tokens >= machinery - 2

    def test_steering_figure_moves_only_when_steering_moves(self, tmp_path: Path) -> None:
        """Editing steering grows the steering figure; the machinery holds.

        This is the distinction the split exists to pin.  The difference
        between the two figures is the base prompt plus block [1b] plus
        the joins between them — none of which the user wrote — and it
        does not drift when a steering file is edited.  (Within one token:
        the heuristic rounds each count up independently.)
        """
        home, ws = _build_workspace(tmp_path, root_file="root: use python3")
        assembler = PromptAssembler(ws, home_dir=home)
        small = assembler.assemble_sync("worker")

        _write(ws / "AGENTS.md", "root: use python3\n" + "prefer explicit imports\n" * 40)
        large = assembler.assemble_sync("worker")

        assert large.steering_tokens > small.steering_tokens
        assert large.prefix_tokens > small.prefix_tokens
        machinery_small = small.prefix_tokens - small.steering_tokens
        machinery_large = large.prefix_tokens - large.steering_tokens
        assert abs(machinery_large - machinery_small) <= 1

    def test_tier_without_steering_reports_zero_not_the_prefix(self, tmp_path: Path) -> None:
        """A validator whose subset matches nothing costs no steering."""
        home = tmp_path / "home"
        home.mkdir()
        ws = tmp_path / "workspace"
        _write(ws / "notes.md", "not a steering file")

        result = PromptAssembler(ws, home_dir=home).assemble_sync("validator", diff="+x")
        assert result.steering.block == ""
        assert result.steering_tokens == 0
        assert result.prefix_tokens > 0
