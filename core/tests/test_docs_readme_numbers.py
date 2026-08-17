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
"""

from __future__ import annotations

import re
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
