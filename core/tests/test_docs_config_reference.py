"""The configuration reference is checked against the loaders (TD-1503).

A configuration reference whose examples do not parse is worse than none —
it teaches a shape the product rejects, and the reader trusts it.  So every
YAML block in ``docs/configuration.md`` carries an HTML comment naming how
it should be checked, and this module runs each one through the real
loaders rather than an imitation of them.

Three kinds of rot are caught here beyond "does it parse":

* **A key that stopped existing.**  Both config models ignore unknown keys
  (pydantic's default), so a stale key in an example validates cleanly and
  documents nothing.  Every mapping in a valid example is checked against
  the model's own field names.
* **A key that never got documented.**  Each field of every config model
  must appear in the prose, so adding one to a schema fails this test until
  the reference catches up.
* **A slug copied out of the shipped config.**  The landscape moves weekly
  (spec §7); a slug pasted into prose is stale by the next release, so the
  reference must point at the shipped config instead of restating it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
import yaml

from tstd.attachments import (
    DEFAULT_MAX_COUNT,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    AttachmentLimits,
)
from tstd.boundary_config import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_SPEND_USD,
    DEFAULT_WALL_CLOCK_HOURS,
    DEFAULT_WRITABLE_PATHS,
    BoundaryConfig,
    BoundarySection,
    CapsSection,
    load_workspace_boundary,
)
from tstd.config import (
    TIER_NAMES,
    ConfigError,
    ModelConfig,
    Preset,
    TierConfig,
    default_config_yaml,
    load_config,
)
from tstd.policy import PolicyConfig, PolicyRule, load_approved_imports, load_policy

ROOT = Path(__file__).resolve().parent.parent.parent
DOC = ROOT / "docs" / "configuration.md"

VALID_KINDS = frozenset({"model", "tier", "workspace"})
INVALID_KINDS = frozenset({"model-invalid", "tier-invalid", "workspace-invalid"})

# `.tst/config.yaml` is one file with several readers, each owning a
# section; the union is what a workspace example may legally declare.
WORKSPACE_SECTIONS = frozenset(
    set(BoundaryConfig.model_fields) | {"policy", "approved_external_imports"}
)

_MARKER = re.compile(r"^<!--\s*verify:\s*([a-z-]+)\s*-->\s*$")
_FENCE = re.compile(r"^```(\w*)\s*$")


class Example:
    """One annotated YAML block: its kind, its text, and where it lives."""

    def __init__(self, kind: str, body: str, line: int) -> None:
        self.kind = kind
        self.body = body
        self.line = line

    def __repr__(self) -> str:  # pragma: no cover - pytest ids only
        return f"{self.kind}@L{self.line}"


def _doc_text() -> str:
    assert DOC.exists(), f"{DOC} is missing"
    return DOC.read_text(encoding="utf-8")


def _examples() -> list[Example]:
    """Every ``yaml`` block in the reference, paired with its verify marker.

    An unmarked block is an error rather than a skip: the point is that no
    example can be added to the reference without also being checked.
    """
    lines = _doc_text().split("\n")
    found: list[Example] = []
    pending: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = _MARKER.match(line)
        if marker:
            pending = marker.group(1)
            index += 1
            continue

        fence = _FENCE.match(line)
        if fence and fence.group(1) == "yaml":
            assert pending is not None, f"{DOC}:{index + 1}: yaml block has no `verify:` marker"
            close = index + 1
            while close < len(lines) and lines[close].strip() != "```":
                close += 1
            assert close < len(lines), f"{DOC}:{index + 1}: unterminated yaml block"
            found.append(Example(pending, "\n".join(lines[index + 1 : close]), index + 1))
            pending = None
            index = close + 1
            continue

        if fence or line.strip():
            # A marker only reaches the block that immediately follows it.
            pending = None
        index += 1

    assert found, "the reference has no verifiable examples"
    return found


EXAMPLES = _examples()
VALID = [e for e in EXAMPLES if e.kind in VALID_KINDS]
INVALID = [e for e in EXAMPLES if e.kind in INVALID_KINDS]


def _wrap_tier(block: str) -> str:
    """Put one tier's mapping into an otherwise minimal config."""
    body: Any = yaml.safe_load(block)
    assert isinstance(body, dict), "a `tier` example must be a mapping of tier keys"
    doc = {"presets": {"demo": dict.fromkeys(TIER_NAMES, body)}, "active_preset": "demo"}
    return yaml.safe_dump(doc, sort_keys=False)


def _load(example: Example, tmp: Path) -> None:
    """Run one example through the loader(s) that own its file."""
    kind = example.kind.removesuffix("-invalid")
    if kind in ("model", "tier"):
        text = example.body if kind == "model" else _wrap_tier(example.body)
        path = tmp / "config.yaml"
        path.write_text(text, encoding="utf-8")
        load_config(path)
        return

    workspace = tmp / "workspace"
    (workspace / ".tst").mkdir(parents=True, exist_ok=True)
    (workspace / ".tst" / "config.yaml").write_text(example.body, encoding="utf-8")
    # One file, three readers — a workspace example has to satisfy all of
    # them, since the daemon runs all three over the same bytes.
    load_workspace_boundary(workspace)
    load_policy(workspace)
    load_approved_imports(workspace)


@pytest.mark.parametrize("example", EXAMPLES, ids=repr)
def test_marker_kinds_are_known(example: Example) -> None:
    assert example.kind in VALID_KINDS | INVALID_KINDS, (
        f"{DOC}:{example.line}: unknown verify kind {example.kind!r}"
    )


@pytest.mark.parametrize("example", VALID, ids=repr)
def test_valid_examples_load(example: Example) -> None:
    """Every example the reference presents as correct actually validates."""
    with TemporaryDirectory() as d:
        try:
            _load(example, Path(d))
        except ConfigError as e:
            raise AssertionError(f"{DOC}:{example.line}: example does not load: {e}") from e


@pytest.mark.parametrize("example", INVALID, ids=repr)
def test_invalid_examples_are_rejected(example: Example) -> None:
    """An example shown as a mistake is still a mistake the loader catches."""
    with TemporaryDirectory() as d, pytest.raises(ConfigError):
        _load(example, Path(d))


_TIER_FIELDS = frozenset(TierConfig.model_fields)
_MODEL_FIELDS = frozenset(ModelConfig.model_fields)
_PRESET_FIELDS = frozenset(Preset.model_fields)
_SECTION_FIELDS = frozenset(BoundarySection.model_fields)
_CAPS_FIELDS = frozenset(CapsSection.model_fields)
_ATTACHMENT_FIELDS = frozenset(AttachmentLimits.model_fields)
_POLICY_FIELDS = frozenset(PolicyConfig.model_fields)
_RULE_FIELDS = frozenset(PolicyRule.model_fields)


def _check_keys(mapping: Any, fields: frozenset[str], where: str) -> None:
    assert isinstance(mapping, dict), f"{where}: expected a mapping"
    unknown = sorted(set(mapping) - fields)
    assert not unknown, f"{where}: key(s) not in the schema: {', '.join(unknown)}"


def _check_workspace(data: dict[str, Any], where: str) -> None:
    _check_keys(data, WORKSPACE_SECTIONS, where)
    if "boundary" in data:
        _check_keys(data["boundary"], _SECTION_FIELDS, f"{where} boundary")
    if "caps" in data:
        _check_keys(data["caps"], _CAPS_FIELDS, f"{where} caps")
    if "attachments" in data:
        _check_keys(data["attachments"], _ATTACHMENT_FIELDS, f"{where} attachments")
    if "policy" in data:
        _check_keys(data["policy"], _POLICY_FIELDS, f"{where} policy")
        for i, rule in enumerate(data["policy"].get("rules", [])):
            _check_keys(rule, _RULE_FIELDS, f"{where} policy.rules[{i}]")


@pytest.mark.parametrize("example", VALID, ids=repr)
def test_example_keys_exist_in_the_schema(example: Example) -> None:
    """Unknown keys are ignored at load, so they need catching here.

    Without this, an example could keep documenting a key that had been
    renamed or removed and every loader check would still pass.
    """
    where = f"{DOC}:{example.line}"
    data: Any = yaml.safe_load(example.body)
    if data is None:
        return  # a comment-only example declares nothing

    if example.kind == "tier":
        _check_keys(data, _TIER_FIELDS, where)
    elif example.kind == "model":
        _check_keys(data, _MODEL_FIELDS, where)
        for name, preset in data.get("presets", {}).items():
            _check_keys(preset, _PRESET_FIELDS, f"{where} presets.{name}")
            for tier, body in preset.items():
                _check_keys(body, _TIER_FIELDS, f"{where} presets.{name}.{tier}")
    else:
        _check_workspace(data, where)


def test_every_config_key_is_documented() -> None:
    """Criterion 1: every key of both files appears in the reference.

    Adding a field to any of these schemas fails this until the reference
    names it — the only way a reference stays complete without an audit.
    """
    text = _doc_text()
    keys = (
        _MODEL_FIELDS
        | _PRESET_FIELDS
        | _TIER_FIELDS
        | WORKSPACE_SECTIONS
        | _SECTION_FIELDS
        | _CAPS_FIELDS
        | _ATTACHMENT_FIELDS
        | _POLICY_FIELDS
        | _RULE_FIELDS
    )
    missing = [key for key in sorted(keys) if f"`{key}`" not in text]
    assert not missing, "undocumented config keys: " + ", ".join(missing)


def test_documented_defaults_match_the_code() -> None:
    """The tables state what the models actually default to."""
    text = _doc_text()
    expected = {
        "writable_paths": json.dumps(list(DEFAULT_WRITABLE_PATHS)),
        "allowed_commands": "[]",
        "network": "deny",
        "spend_usd": str(DEFAULT_SPEND_USD),
        "wall_clock_hours": str(DEFAULT_WALL_CLOCK_HOURS),
        "max_iterations": str(DEFAULT_MAX_ITERATIONS),
        "max_file_bytes": str(DEFAULT_MAX_FILE_BYTES),
        "max_total_bytes": str(DEFAULT_MAX_TOTAL_BYTES),
        "max_count": str(DEFAULT_MAX_COUNT),
        "rules": "[]",
        "class_c_default": str(PolicyConfig.model_fields["class_c_default"].default),
    }
    for key, value in expected.items():
        rows = [line for line in text.split("\n") if line.startswith(f"| `{key}`")]
        assert rows, f"no table row for `{key}`"
        assert any(value in row for row in rows), (
            f"the `{key}` row does not state its real default {value!r}"
        )


def test_shipped_preset_and_tier_names_are_documented() -> None:
    """Preset and tier names are identifiers, not moving values — name them."""
    text = _doc_text()
    shipped = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
    for name in shipped.presets:
        assert f"`{name}`" in text, f"shipped preset `{name}` is not documented"
    for tier in TIER_NAMES:
        assert f"`{tier}`" in text, f"tier `{tier}` is not documented"


def test_no_shipped_slug_is_copied_into_the_prose() -> None:
    """Slugs rot.  The reference points at the shipped config instead.

    Restating one here means two places to update when the landscape moves,
    and the doc is the one that gets forgotten.
    """
    text = _doc_text()
    shipped = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
    copied = sorted(
        {
            tier.slug
            for preset in shipped.presets.values()
            for tier in (preset.brain, preset.worker, preset.validator)
            if tier.slug is not None and tier.slug in text
        }
    )
    assert not copied, "shipped slugs copied into the reference: " + ", ".join(copied)


def test_absent_workspace_file_means_the_documented_defaults() -> None:
    """The '§4 an absent file means the defaults' claim, checked not asserted."""
    with TemporaryDirectory() as d:
        workspace = Path(d)
        assert load_workspace_boundary(workspace) == BoundaryConfig()
        assert load_policy(workspace) == PolicyConfig()
        assert load_approved_imports(workspace) == frozenset()
