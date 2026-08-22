"""The README's cost story is checked against the shipped config (TD-1501).

The README is the one document a stranger reads before deciding whether to
trust the project, and its cost section is the part that has to be true.
Hand-typed dollar figures rot the first time a price moves in
``config.yaml``, and nothing else in the suite would notice — the README is
not imported by anything.

So none of it is taken on trust, following the pattern
``test_docs_config_reference``, ``test_docs_steering_guide`` and
``test_docs_architecture_guide`` already use:

* **The price table is derived, in both directions.**  Every tier of every
  shipped preset must have a row, and every row must match the tier's real
  ``input_price`` / ``output_price`` / ``cache_read_price``.  Adding a
  preset fails this until the README names it; changing a price fails it
  until the README follows.
* **The worked example is executed**, not asserted — its four dollar
  figures are recomputed by the shipped ``compute_call_details`` from the
  token counts the README itself states.
* **The per-session estimate is traced to its source.**  It is a spec
  figure, not a measurement, so the README has to quote the spec exactly
  rather than drift its own version of it.
* **No slug is copied in.**  Same reasoning as the configuration
  reference: the landscape moves weekly, so prose points at the shipped
  config instead of restating it.
* **Status claims follow this checkout's exit harnesses** (TD-4811).
  Placement is derived from the backlog checkboxes on *this* tree, not a
  frozen "these milestones have shipped" table — that is how an earlier
  rewrite locked in a false M7/M8 gap.
"""

from __future__ import annotations

import re
import subprocess
from importlib import import_module
from pathlib import Path

import pytest
import yaml

from tstd.config import TIER_NAMES, ModelConfig, Preset, TierConfig, default_config_yaml
from tstd.cost import compute_call_details
from tstd.provider import Usage

ROOT = Path(__file__).resolve().parent.parent.parent
DOC = ROOT / "README.md"
SPEC = ROOT / "docs" / "tst-desk-spec.md"

# | `tst-default` | `brain` | $2.80 | $14.00 | $0.30 |
_PRICE_ROW = re.compile(
    r"^\|\s*`(?P<preset>[\w-]+)`\s*\|\s*`(?P<tier>\w+)`"
    r"\s*\|\s*\$(?P<input>[\d.]+)"
    r"\s*\|\s*\$(?P<output>[\d.]+)"
    r"\s*\|\s*\$(?P<cache>[\d.]+)\s*\|\s*$",
    re.MULTILINE,
)
# A worked-example line: token count, component name, rate, then its dollar cost.
_EXAMPLE_LINE = re.compile(
    r"^\s*(?P<tokens>[\d,]+)\s+(?P<kind>uncached prompt|cached prompt|completion)\b"
    r".*?\$(?P<cost>[\d.]+)\s*$",
    re.MULTILINE,
)
_EXAMPLE_TOTAL = re.compile(r"^\s*\$(?P<total>[\d.]+)\s*$", re.MULTILINE)
_SESSION_ESTIMATE = re.compile(r"\*\*\$(?P<usd>[\d.]+)\s+per\s+working\s+session\*\*")


def _doc_text() -> str:
    assert DOC.exists(), f"{DOC} is missing"
    return DOC.read_text(encoding="utf-8")


def _shipped() -> ModelConfig:
    return ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))


def _tiers(preset: Preset) -> dict[str, TierConfig]:
    return {"brain": preset.brain, "worker": preset.worker, "validator": preset.validator}


def _documented_prices() -> dict[tuple[str, str], tuple[float, float, float]]:
    """Every price row in the README, keyed by ``(preset, tier)``."""
    rows: dict[tuple[str, str], tuple[float, float, float]] = {}
    for m in _PRICE_ROW.finditer(_doc_text()):
        key = (m.group("preset"), m.group("tier"))
        assert key not in rows, f"{DOC}: duplicate price row for {key[0]}/{key[1]}"
        rows[key] = (float(m.group("input")), float(m.group("output")), float(m.group("cache")))
    assert rows, f"{DOC}: no price rows found — has the cost table been reformatted?"
    return rows


SHIPPED_TIERS = [
    (preset_name, tier_name)
    for preset_name, preset in _shipped().presets.items()
    for tier_name in TIER_NAMES
]


@pytest.mark.parametrize(("preset_name", "tier_name"), SHIPPED_TIERS)
def test_documented_prices_match_the_shipped_config(preset_name: str, tier_name: str) -> None:
    """Every shipped tier has a row, and the row states its real prices."""
    documented = _documented_prices()
    row = documented.get((preset_name, tier_name))
    assert row is not None, (
        f"{DOC}: no cost-table row for preset `{preset_name}` tier `{tier_name}`"
    )
    tier = _tiers(_shipped().presets[preset_name])[tier_name]
    expected = (tier.input_price, tier.output_price, tier.cache_read_price)
    assert row == expected, (
        f"{DOC}: `{preset_name}`/`{tier_name}` states {row}, config.yaml says {expected}"
    )


def test_no_row_names_a_preset_or_tier_that_stopped_existing() -> None:
    """The other direction: a removed preset leaves a row behind."""
    shipped = _shipped()
    for preset_name, tier_name in _documented_prices():
        assert preset_name in shipped.presets, (
            f"{DOC}: cost table documents preset `{preset_name}`, which config.yaml does not ship"
        )
        assert tier_name in TIER_NAMES, f"{DOC}: cost table documents unknown tier `{tier_name}`"


def test_the_worked_example_is_what_the_cost_function_computes() -> None:
    """The example's arithmetic is run, not trusted.

    It is stated against `tst-default`'s brain, so the prices come from
    there and the token counts come from the README's own prose.
    """
    text = _doc_text()
    tokens = {
        m.group("kind"): (int(m.group("tokens").replace(",", "")), float(m.group("cost")))
        for m in _EXAMPLE_LINE.finditer(text)
    }
    assert set(tokens) == {"uncached prompt", "cached prompt", "completion"}, (
        f"{DOC}: the worked example should have one line per cost component, found {sorted(tokens)}"
    )

    uncached, uncached_cost = tokens["uncached prompt"]
    cached, cached_cost = tokens["cached prompt"]
    completion, completion_cost = tokens["completion"]
    usage = Usage(
        prompt_tokens=uncached + cached,
        cached_prompt_tokens=cached,
        completion_tokens=completion,
        total_tokens=uncached + cached + completion,
    )
    actual = compute_call_details(usage, _tiers(_shipped().presets["tst-default"])["brain"])

    assert uncached_cost == actual["prompt_cost"], "uncached prompt line"
    assert cached_cost == actual["cached_cost"], "cached prompt line"
    assert completion_cost == actual["completion_cost"], "completion line"

    totals = _EXAMPLE_TOTAL.findall(text)
    assert totals, f"{DOC}: the worked example states no total"
    assert float(totals[-1]) == actual["total_cost"], (
        f"{DOC}: the worked example totals ${totals[-1]}, the cost function says "
        f"${actual['total_cost']}"
    )


def test_the_session_estimate_is_quoted_from_the_spec() -> None:
    """It is a design-time figure, so it must match the document it cites."""
    match = _SESSION_ESTIMATE.search(_doc_text())
    assert match is not None, f"{DOC}: the per-session estimate is not stated in the quoted form"
    quoted = f"${match.group('usd')} per working session"
    assert quoted in SPEC.read_text(encoding="utf-8"), (
        f"{DOC}: claims '{quoted}', which {SPEC.name} does not say"
    )


def test_no_shipped_slug_is_copied_into_the_readme() -> None:
    """Slugs rot.  The README points at the shipped config instead."""
    text = _doc_text()
    copied = sorted(
        {
            tier.slug
            for preset in _shipped().presets.values()
            for tier in _tiers(preset).values()
            if tier.slug is not None and tier.slug in text
        }
    )
    assert not copied, "shipped slugs copied into the README: " + ", ".join(copied)


_README_IMAGE = re.compile(r"!\[[^\]]*\]\((?P<path>docs/images/[^)]+)\)")


def test_the_readme_embeds_a_real_window_capture() -> None:
    """The screenshot is the last TD-1501 gap; a broken path would hide it."""
    match = _README_IMAGE.search(_doc_text())
    assert match is not None, f"{DOC}: no markdown image pointing at docs/images/"
    image = ROOT / match.group("path")
    assert image.is_file(), f"{DOC}: embeds {match.group('path')}, which is not in the tree"
    assert image.stat().st_size > 10_000, f"{image}: too small to be a window capture"


# ---------------------------------------------------------------------------
# The Status section (TD-4811).  Same contract as the price table: derived
# from what this checkout actually shipped, never taken on trust.
# ---------------------------------------------------------------------------

BACKLOG = ROOT / "docs" / "tst-desk-backlog.md"
_MCP_BACKENDS = ROOT / "mcp" / "tst-cu-mcp" / "src" / "tst_cu_mcp" / "backends" / "__init__.py"

_MILESTONE_RE = re.compile(r"^# MILESTONE (?P<name>M[\d.]+)", re.MULTILINE)
_STORY_RE = re.compile(r"^### (?P<id>TD-\d+) —", re.MULTILINE)
_WORKS_ANCHOR = "What works:"
_NOT_BUILT_ANCHOR = "What is not built yet"

# Keywords that must sit in the works list once the backlog ticks that
# milestone's exit harness, and in the not-built list while it is open.
# Phrases are chosen so an exited theme cannot be a substring of an open
# leftover (``computer-use`` vs ``Linux desktop capture``; ``mcp`` vs
# ``MCP loading`` — works names ``tst-cu-mcp``).
MILESTONE_THEMES: dict[str, tuple[str, ...]] = {
    "M1.5": ("keyless",),
    "M4": ("agent memory", "revive"),
    "M5": ("closed window", "tst run"),
    "M6": ("computer-use", "kill switch"),
    "M7": ("remote attach", "tailscale"),
    "M8": ("vllm", "ui-tars"),
    "M9": ("autonomy engine",),
    "M10": ("slash commands", "SKILL.md", "plan lock", "MCP loading"),
}

# Works-list phrases -> importable modules that make the claim true.
# A ticked checkbox with a deleted module is still a lie.
_WORKS_IMPORTS: tuple[tuple[str, str], ...] = (
    ("agent memory", "tstd.memory_store"),
    ("agent memory", "tstd.context.memory_loader"),
    ("tst run", "tstd.cli"),
    ("computer-use", "tstd.tools.desktop"),
    ("remote attach", "tstd.remote_attach"),
    ("tailscale", "tstd.tailscale_bind"),
    ("ui-tars", "tstd.desktop.grounding_client"),
    ("charter.md", "tstd.autonomy.charter"),
    ("charter editor", "tstd.autonomy.charter_io"),
)

# Not-built leftovers that are stories, not milestone exits. If Status
# names the gap, the story must still have an open box on this checkout.
_LEFTOVER_STORIES: dict[str, str] = {
    "linux desktop": "TD-2001",
    "design-mode": "TD-3406",
    "charter editor": "TD-4002",
}


def _milestone_exit_state() -> dict[str, bool]:
    """Milestone name -> whether its exit-criterion box is ticked.

    Only milestones that state an exit harness in their own section get
    an entry. The M1 harness lives under M3, so M1 is skipped — the
    needle is ``This harness is the <section-name> exit criterion``.
    """
    text = BACKLOG.read_text(encoding="utf-8")
    marks = list(_MILESTONE_RE.finditer(text))
    assert marks, f"{BACKLOG}: no '# MILESTONE' headers found"
    state: dict[str, bool] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        chunk = text[mark.start() : end]
        needle = f"This harness is the {mark.group('name')} exit criterion"
        idx = chunk.find(needle)
        if idx == -1:
            continue
        line_start = chunk.rfind("\n", 0, idx) + 1
        line_end = chunk.find("\n", idx)
        line = chunk[line_start : line_end if line_end != -1 else len(chunk)]
        assert "- [x]" in line or "- [ ]" in line, (
            f"{BACKLOG}: {mark.group('name')}'s exit-harness line lost its checkbox: {line!r}"
        )
        state[mark.group("name")] = "- [x]" in line
    return state


def _story_chunk(story_id: str) -> str:
    text = BACKLOG.read_text(encoding="utf-8")
    marks = list(_STORY_RE.finditer(text))
    for i, mark in enumerate(marks):
        if mark.group("id") != story_id:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        return text[mark.start() : end]
    raise AssertionError(f"{BACKLOG}: no '### {story_id} —' header")


def _status_text() -> str:
    """The Status section's body, from its header to the next section."""
    match = re.search(r"^## Status\n(.*?)(?=^## )", _doc_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"{DOC}: the '## Status' section is missing or renamed"
    return match.group(1)


def _status_halves() -> tuple[str, str]:
    """The works-list half and the not-built half of the Status section.

    Close-vs-Quit is a ``###`` under Status and must not pollute the
    not-built half. Markdown hard-wraps mid-phrase, so both halves are
    collapsed to single spaces before keyword matching.
    """
    status = _status_text()
    works_at = status.find(_WORKS_ANCHOR)
    assert works_at != -1, f"{DOC}: Status no longer says '{_WORKS_ANCHOR}' — reformatted?"
    not_built_at = status.find(_NOT_BUILT_ANCHOR)
    assert not_built_at != -1, f"{DOC}: Status no longer says '{_NOT_BUILT_ANCHOR}'"
    assert works_at < not_built_at, f"{DOC}: the works list must come before the not-built list"

    not_built_block = status[not_built_at:]
    next_sub = re.search(r"\n### ", not_built_block)
    if next_sub is not None:
        not_built_block = not_built_block[: next_sub.start()]

    def flatten(part: str) -> str:
        return " ".join(part.split())

    return flatten(status[works_at:not_built_at]), flatten(not_built_block)


def test_every_exit_harness_has_a_status_theme() -> None:
    """A new milestone exit must get a row, or Status can silently skip it."""
    state = _milestone_exit_state()
    missing = sorted(set(state) - set(MILESTONE_THEMES))
    extra = sorted(set(MILESTONE_THEMES) - set(state))
    assert not missing, (
        f"{BACKLOG}: exit harnesses with no Status theme — add them to MILESTONE_THEMES: {missing}"
    )
    assert not extra, (
        f"MILESTONE_THEMES names {extra}, which have no exit-harness line on this checkout"
    )


@pytest.mark.parametrize("milestone", sorted(MILESTONE_THEMES))
def test_status_follows_this_checkouts_exit_harness(milestone: str) -> None:
    """Ticked exit → works list; open exit → not-built list. Never the reverse."""
    state = _milestone_exit_state()
    assert milestone in state, (
        f"{BACKLOG}: {milestone} has no exit-harness line — drop it from MILESTONE_THEMES"
    )
    works, not_built = _status_halves()
    exited = state[milestone]
    claimed, forbidden = (works, not_built) if exited else (not_built, works)
    side = "works" if exited else "not-built"
    other = "not-built" if exited else "works"
    for keyword in MILESTONE_THEMES[milestone]:
        assert keyword.lower() in claimed.lower(), (
            f"{DOC}: {milestone} exit is {'ticked' if exited else 'open'} but the "
            f"{side} list never mentions {keyword!r}"
        )
        assert keyword.lower() not in forbidden.lower(), (
            f"{DOC}: the {other} list names {keyword!r}, but {milestone} "
            f"{'has exited' if exited else 'has not exited'}"
        )


def test_not_built_does_not_repeat_the_false_m7_m8_gap() -> None:
    """c8fb7c6 claimed remote/vLLM were missing after M7/M8 had already exited."""
    state = _milestone_exit_state()
    _, not_built = _status_halves()
    lowered = not_built.lower()
    if state.get("M7"):
        assert "remote attach" not in lowered
    if state.get("M8"):
        assert "vllm" not in lowered
        assert "ezer" not in lowered
        assert "ui-tars" not in lowered


def test_works_claims_resolve_to_shipped_modules() -> None:
    """A works-list phrase is not enough — the module that backs it must import."""
    works, _ = _status_halves()
    lowered = works.lower()
    for phrase, module in _WORKS_IMPORTS:
        if phrase.lower() not in lowered:
            continue
        import_module(module)
    if "vllm" in lowered:
        assert "vllm" in _shipped().presets, f"{DOC}: works names vllm, but no such preset ships"


@pytest.mark.parametrize("phrase,story_id", sorted(_LEFTOVER_STORIES.items()))
def test_named_leftovers_are_still_open_stories(phrase: str, story_id: str) -> None:
    """If Status names a leftover gap, that story's AC must still have an open box."""
    _, not_built = _status_halves()
    if phrase.lower() not in not_built.lower():
        pytest.skip(f"{DOC}: not-built list does not name {phrase!r}")
    chunk = _story_chunk(story_id)
    assert "- [ ]" in chunk, (
        f"{DOC}: not-built names {phrase!r} ({story_id}), but every AC box on that "
        "story is ticked — move the claim to the works list"
    )


def test_linux_desktop_gap_matches_the_sidecar() -> None:
    """Linux desktop CU is a real hole only while the sidecar omits linux."""
    _, not_built = _status_halves()
    if "linux desktop" not in not_built.lower():
        pytest.skip(f"{DOC}: not-built list does not name the Linux desktop gap")
    assert _MCP_BACKENDS.is_file(), f"{_MCP_BACKENDS}: sidecar backends module missing"
    match = re.search(r"SUPPORTED_PLATFORMS\s*=\s*\((?P<body>[^)]*)\)", _MCP_BACKENDS.read_text())
    assert match is not None, f"{_MCP_BACKENDS}: SUPPORTED_PLATFORMS not found"
    platforms = re.findall(r'["\'](\w+)["\']', match.group("body"))
    assert "linux" not in platforms, (
        f"{DOC}: not-built names Linux desktop capture, but {platforms} already includes linux"
    )


def _git_v_tags() -> tuple[list[str], bool]:
    """Every ``v*`` tag known to git, plus whether the clone is shallow."""
    shallow_out = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-shallow-repository"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tags_out = subprocess.run(
        ["git", "-C", str(ROOT), "tag", "--list", "v*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(t for t in tags_out.splitlines() if t), shallow_out == "true"


def test_the_release_claim_matches_the_repository_tags() -> None:
    """The tag sentence is checkable with plain git, so check it with git.

    Pushing (or deleting) a ``v*`` tag without touching the Status section
    fails here — which is the point: the release story (TD-1302/TD-1303)
    and this sentence move together.
    """
    tags, shallow = _git_v_tags()
    if not tags and shallow:
        pytest.skip("shallow clone without fetched tags cannot audit the release claim")
    mentioned = set(re.findall(r"`(v[\d.]+)`", _status_text()))
    assert set(tags) == mentioned, (
        f"{DOC}: the release sentence names tags {sorted(mentioned)}, git tag lists {tags} — "
        "they must agree"
    )
