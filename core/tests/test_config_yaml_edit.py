"""TD-5203: one key of config.yaml changes and every other byte stays.

The splicer edits text by the positions PyYAML's composer reports, so these
tests compare whole files: what changed must be exactly the edited key, and
the value must read back as written.
"""

from __future__ import annotations

import difflib
from typing import Any

import pytest
import yaml

from tstd.config import default_config_yaml
from tstd.config_yaml_edit import remove_value, set_value, top_level_block
from tstd.config_yaml_emit import YamlEditError, scalar_text

DOC = """\
# A teaching comment that must survive.
search:
  base_url: https://example.invalid/html/   # trailing note
  timeout_seconds: 15
  fallback_base_urls:
    - https://one.invalid/
    - https://two.invalid/
  # a comment after the list

consult:
  tiers: [worker]
  max_per_turn: 2

mcp:
  servers: {}

hooks:
  after_write: []

local_servers:
  - name: Ollama
    base_url: http://127.0.0.1:11434/v1
  - name: vLLM
    base_url: http://127.0.0.1:8000/v1

compaction:
  summary_prompt: |
    line one
    line two

  summary: model
presets:
end: 1
"""


def _get(data: Any, path: list[Any]) -> Any:
    for part in path:
        data = data[part]
    return data


def _changed(old: str, new: str) -> list[str]:
    """Lines removed (``-``) and added (``+``), in order."""
    return [
        line
        for line in difflib.ndiff(old.splitlines(), new.splitlines())
        if line.startswith(("- ", "+ "))
    ]


def _round_trip(text: str, path: list[Any], value: Any, **kw: Any) -> str:
    out = set_value(text, path, value, **kw)
    assert _get(yaml.safe_load(out), path) == value
    return out


def test_a_scalar_changes_on_its_own_line_and_keeps_its_trailing_comment() -> None:
    out = _round_trip(DOC, ["search", "base_url"], "https://new.invalid/")
    assert _changed(DOC, out) == [
        "-   base_url: https://example.invalid/html/   # trailing note",
        '+   base_url: "https://new.invalid/"   # trailing note',
    ]


def test_numbers_strings_and_null_read_back_as_written() -> None:
    for value in (20, 2.5, 1e-05, 1e100, True, None, "on", "2026-10-09", "a: b # c", "é ✓ 🚀"):
        out = _round_trip(DOC, ["search", "timeout_seconds"], value)
        assert len(_changed(DOC, out)) == 2, value


def test_line_breaks_and_control_characters_stay_inside_the_string() -> None:
    value = 'one\ntwo\U00002028three\x85four\x7f\ttab \\ "quote"'
    out = _round_trip(DOC, ["search", "base_url"], value)
    assert len(_changed(DOC, out)) == 2


def test_a_float_in_exponent_form_keeps_a_decimal_point() -> None:
    # YAML 1.1 reads 1e-05 as a string; 1.0e-05 is a float.
    assert scalar_text(1e-05) == "1.0e-05"
    assert isinstance(yaml.safe_load(scalar_text(1e-05)), float)
    with pytest.raises(YamlEditError):
        scalar_text(float("nan"))


def test_a_block_list_stays_block_and_the_comment_after_it_stays() -> None:
    out = _round_trip(DOC, ["search", "fallback_base_urls"], ["https://two.invalid/"])
    assert _changed(DOC, out) == [
        "-     - https://one.invalid/",
        "-     - https://two.invalid/",
        '+     - "https://two.invalid/"',
    ]
    assert "  # a comment after the list\n" in out


def test_a_flow_list_stays_flow() -> None:
    out = _round_trip(DOC, ["consult", "tiers"], ["worker", "brain"])
    assert _changed(DOC, out) == ["-   tiers: [worker]", '+   tiers: ["worker", "brain"]']


def test_json_values_are_written_on_one_line() -> None:
    value = {"chat_template_kwargs": {"enable_thinking": False}}
    out = _round_trip(DOC, ["consult", "extra"], value, flow=True)
    assert '  extra: {"chat_template_kwargs": {"enable_thinking": false}}' in out


def test_a_missing_key_is_added_at_the_end_of_its_mapping() -> None:
    out = _round_trip(DOC, ["consult", "max_tasks"], 4)
    assert _changed(DOC, out) == ["+   max_tasks: 4"]
    assert out.index("max_tasks: 4") > out.index("max_per_turn: 2")
    assert out.index("max_tasks: 4") < out.index("mcp:")


def test_missing_parents_are_created_in_block_style() -> None:
    out = _round_trip(DOC, ["notify", "slack", "enabled"], True)
    assert out.endswith("end: 1\n\nnotify:\n  slack:\n    enabled: true\n")


def test_an_empty_flow_map_opens_into_a_block() -> None:
    out = _round_trip(DOC, ["mcp", "servers", "docs", "transport"], "stdio")
    assert 'mcp:\n  servers:\n    docs:\n      transport: "stdio"\n\nhooks:' in out


def test_appending_to_an_empty_flow_list_opens_it_into_a_block() -> None:
    hook = {"command": ["ruff", "format"], "match": ["*.py"]}
    out = _round_trip(DOC, ["hooks", "after_write", 0], hook)
    assert (
        'hooks:\n  after_write:\n    - command:\n        - "ruff"\n        - "format"\n'
        '      match:\n        - "*.py"\n' in out
    )


def test_appending_to_a_block_list_of_maps_adds_one_item() -> None:
    item = {"name": "LM Studio", "base_url": "http://127.0.0.1:1234/v1"}
    out = _round_trip(DOC, ["local_servers", 2], item)
    assert _changed(DOC, out) == [
        '+   - name: "LM Studio"',
        '+     base_url: "http://127.0.0.1:1234/v1"',
    ]


def test_an_item_field_is_set_in_place() -> None:
    out = _round_trip(DOC, ["local_servers", 1, "name"], "vLLM 2")
    assert _changed(DOC, out) == ["-   - name: vLLM", '+   - name: "vLLM 2"']


def test_a_block_scalar_is_replaced_without_eating_the_next_key() -> None:
    out = _round_trip(DOC, ["compaction", "summary_prompt"], "short")
    assert '  summary_prompt: "short"\n\n  summary: model\n' in out


def test_a_null_parent_becomes_a_mapping() -> None:
    out = _round_trip(DOC, ["presets", "mine", "label"], "Mine")
    assert 'presets:\n  mine:\n    label: "Mine"\nend: 1\n' in out


def test_removing_a_key_drops_its_lines_only() -> None:
    out = remove_value(DOC, ["search", "fallback_base_urls"])
    assert _changed(DOC, out) == [
        "-   fallback_base_urls:",
        "-     - https://one.invalid/",
        "-     - https://two.invalid/",
    ]
    assert "fallback_base_urls" not in yaml.safe_load(out)["search"]


def test_removing_the_first_key_of_a_list_item_keeps_the_item() -> None:
    out = remove_value(DOC, ["local_servers", 0, "name"])
    assert yaml.safe_load(out)["local_servers"][0] == {"base_url": "http://127.0.0.1:11434/v1"}
    assert _changed(DOC, out) == [
        "-   - name: Ollama",
        "-     base_url: http://127.0.0.1:11434/v1",
        "+   - base_url: http://127.0.0.1:11434/v1",
    ]


def test_removing_a_list_item_and_the_last_one() -> None:
    out = remove_value(DOC, ["local_servers", 0])
    assert [s["name"] for s in yaml.safe_load(out)["local_servers"]] == ["vLLM"]
    out = remove_value(out, ["local_servers", 0])
    assert yaml.safe_load(out)["local_servers"] == []
    assert "local_servers: []\n" in out


def test_removing_the_only_key_leaves_an_empty_map_not_null() -> None:
    text = "notify:\n  slack:\n    enabled: true\nother: 1\n"
    out = remove_value(text, ["notify", "slack", "enabled"])
    assert out == "notify:\n  slack: {}\nother: 1\n"


def test_removing_from_flow_collections() -> None:
    text = "a: {x: 1, y: 2, z: 3}\nb: [1, 2, 3]\n"
    assert remove_value(text, ["a", "y"]) == "a: {x: 1, z: 3}\nb: [1, 2, 3]\n"
    assert remove_value(text, ["a", "z"]) == "a: {x: 1, y: 2}\nb: [1, 2, 3]\n"
    assert remove_value(text, ["b", 2]) == "a: {x: 1, y: 2, z: 3}\nb: [1, 2]\n"
    assert remove_value(text, ["b", 0]) == "a: {x: 1, y: 2, z: 3}\nb: [2, 3]\n"


def test_removing_a_missing_key_changes_nothing() -> None:
    assert remove_value(DOC, ["search", "nope"]) == DOC
    assert remove_value(DOC, ["nowhere", "at", "all"]) == DOC


def test_windows_line_endings_are_kept() -> None:
    text = DOC.replace("\n", "\r\n")
    out = _round_trip(text, ["consult", "max_tasks"], 4)
    assert "\r\n  max_tasks: 4\r\n" in out
    assert "\n" not in out.replace("\r\n", "")
    out = remove_value(out, ["search", "timeout_seconds"])
    assert "\n" not in out.replace("\r\n", "")


def test_a_key_behind_an_anchor_is_refused() -> None:
    text = "base: &tier {slug: a}\nother: *tier\n"
    with pytest.raises(YamlEditError, match="anchor or alias"):
        set_value(text, ["other", "slug"], "b")


def test_a_comment_only_document_gets_its_first_key() -> None:
    out = _round_trip("# nothing yet\n", ["engine", "kind"], "native")
    assert out == '# nothing yet\nengine:\n  kind: "native"\n'


def test_a_key_that_yaml_would_read_as_a_boolean_is_quoted() -> None:
    out = _round_trip(DOC, ["presets", "no", "label"], "x")
    assert yaml.safe_load(out)["presets"] == {"no": {"label": "x"}}


def test_the_shipped_block_comes_with_its_comment() -> None:
    block = top_level_block(default_config_yaml(), "workflow")
    assert block is not None
    assert block.startswith("# The workflow tool (TD-5195)")
    assert block.endswith("  max_tasks: 8")
    assert yaml.safe_load(block) == {"workflow": {"max_parallel": 3, "max_tasks": 8}}


def test_every_shipped_key_round_trips_in_the_shipped_file() -> None:
    """Rewriting each shipped scalar to its own value changes one line at most."""
    text = default_config_yaml()
    data = yaml.safe_load(text)

    def leaves(node: Any, path: list[Any]) -> list[tuple[list[Any], Any]]:
        if isinstance(node, dict):
            return [leaf for k, v in node.items() for leaf in leaves(v, [*path, k])]
        if isinstance(node, list):
            return [leaf for i, v in enumerate(node) for leaf in leaves(v, [*path, i])]
        return [(path, node)]

    for path, value in leaves(data, []):
        out = _round_trip(text, path, value)
        assert len(_changed(text, out)) <= 2, path


def test_flow_collections_grow_in_flow_style() -> None:
    text = "notify: {slack: {enabled: false}}\nconsult: {tiers: [worker]}\n"
    out = _round_trip(text, ["notify", "slack", "host"], "hooks.example.com")
    assert out.startswith('notify: {slack: {enabled: false, "host": "hooks.example.com"}}\n')
    out = _round_trip(out, ["consult", "tiers", 1], "brain")
    assert out.endswith('consult: {tiers: [worker, "brain"]}\n')


def test_removing_a_key_keeps_the_comment_lines_after_it() -> None:
    text = "a:\n  b: 1\n  c:\n    - x\n  # about d\n  d: 2\n"
    assert remove_value(text, ["a", "c"]) == "a:\n  b: 1\n  # about d\n  d: 2\n"


SHIPPED_HOOK = (
    "hooks:\n"
    "  after_write:\n"
    '    - {name: ruff format, command: "ruff format {files}", match: ["*.py"]}\n'
    "lsp:\n  servers: []\n"
)
FLOW_TIER = (
    "presets:\n  local:\n"
    '    worker: { base_url: "http://127.0.0.1:11434/v1", max_output_tokens: 4096 }\n'
    "end: 1\n"
)


def test_a_key_inside_a_flow_map_is_written_in_flow_style() -> None:
    """The shipped example hook line, uncommented, then its command made a list."""
    words = ["ruff", "format", "--quiet", "{files}"]
    out = _round_trip(SHIPPED_HOOK, ["hooks", "after_write", 0, "command"], words)
    assert _changed(SHIPPED_HOOK, out) == [
        '-     - {name: ruff format, command: "ruff format {files}", match: ["*.py"]}',
        '+     - {name: ruff format, command: ["ruff", "format", "--quiet", "{files}"], '
        'match: ["*.py"]}',
    ]


def test_a_flow_tier_empties_and_refills_its_lists_in_flow_style() -> None:
    backup = {"base_url": "http://127.0.0.1:8000/v1", "slug": "vendor/spare"}
    fallbacks = ["presets", "local", "worker", "fallbacks"]
    out = _round_trip(FLOW_TIER, [*fallbacks, 0], backup)
    out = remove_value(out, [*fallbacks, 0])
    assert yaml.safe_load(out)["presets"]["local"]["worker"]["fallbacks"] == []
    out = _round_trip(out, [*fallbacks, 0], backup)
    tools = ["presets", "local", "worker", "deferred_tools"]
    for value in (["x"], [], ["fs_read"]):
        out = _round_trip(out, tools, value)
    out = _round_trip(out, ["presets", "local", "worker", "extra_body"], {})
    out = _round_trip(out, ["presets", "local", "worker", "extra_body", "seed"], 7)
    out = _round_trip(out, ["presets", "local", "worker", "reply_guard"], None)
    out = _round_trip(out, ["presets", "local", "worker", "reply_guard", "retries"], 3)
    # Still one line: nothing was opened into block style inside the braces.
    assert len(out.splitlines()) == len(FLOW_TIER.splitlines())


@pytest.mark.parametrize(
    "text",
    [
        "base: &tier {slug: a}\nother:\n  <<: *tier\n  label: x\n",
        "other:\n  <<: {slug: a}\n  label: x\n",
    ],
)
def test_a_key_in_a_mapping_with_a_merge_is_refused(text: str) -> None:
    with pytest.raises(YamlEditError, match="merge"):
        set_value(text, ["other", "label"], "y")
    with pytest.raises(YamlEditError, match="merge"):
        remove_value(text, ["other", "label"])


def test_a_key_listed_twice_is_refused_rather_than_edited_in_the_wrong_copy() -> None:
    text = "search:\n  max_results: 8\nsearch:\n  max_results: 3\n"
    with pytest.raises(YamlEditError, match="lists search twice"):
        set_value(text, ["search", "max_results"], 5)
    with pytest.raises(YamlEditError, match="lists search twice"):
        remove_value(text, ["search", "max_results"])


def test_removing_the_first_key_of_an_item_keeps_the_comment_about_the_next() -> None:
    text = (
        "hooks:\n  after_write:\n"
        "    - name: ruff format   # shown on the step\n"
        "      # run the formatter before the linter\n"
        '      command: ["ruff", "format", "{files}"]\n'
    )
    out = remove_value(text, ["hooks", "after_write", 0, "name"])
    assert "      # run the formatter before the linter\n" in out
    assert yaml.safe_load(out)["hooks"]["after_write"] == [
        {"command": ["ruff", "format", "{files}"]}
    ]
    assert '    - command: ["ruff", "format", "{files}"]\n' in out


def test_a_block_prompt_stays_a_block() -> None:
    out = _round_trip(DOC, ["compaction", "summary_prompt"], "line one\nline three\n")
    assert "  summary_prompt: |\n    line one\n    line three\n\n  summary: model\n" in out
    out = _round_trip(DOC, ["compaction", "summary_prompt"], "no break at the end\nhere")
    assert "  summary_prompt: |-\n    no break at the end\n    here\n" in out
    text = "a:\n  p: |  # keep this note\n    old\n  q: 1\n"
    out = _round_trip(text, ["a", "p"], "new\ntext\n")
    assert out == "a:\n  p: |  # keep this note\n    new\n    text\n  q: 1\n"
    # Text a block cannot hold exactly is written quoted instead.
    for value in ("  indented first\nline\n", "trailing blanks\n\n", "tab\there\n\x07"):
        out = _round_trip(DOC, ["compaction", "summary_prompt"], value)
        assert "summary_prompt: |" not in out, value


@pytest.mark.parametrize("mark", ["\u2028", "\u2029", "\x85"])
def test_a_unicode_line_separator_is_refused_before_it_can_hide_a_key(mark: str) -> None:
    text = f"search:\n  timeout_seconds: 15 # pasted note{mark}  max_results: 3\n  other: 1\n"
    with pytest.raises(YamlEditError, match="line separator"):
        remove_value(text, ["search", "timeout_seconds"])
    with pytest.raises(YamlEditError, match="line separator"):
        set_value(text, ["search", "other"], 2)
