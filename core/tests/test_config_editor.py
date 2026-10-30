"""TD-5203: one Settings → Configuration edit, validated before it is written.

Each test edits a real config.yaml (the shipped file with the test catalog)
through ``apply_edit`` and checks the file afterwards: the edited key reads
back, everything else is byte-identical, and a refused edit leaves the
bytes exactly as they were.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tstd.config import ensure_user_config, load_config
from tstd.config_editor import (
    apply_edit,
    build_view,
    path_key,
    shipped_defaults,
    version_of,
)
from tstd.config_editor_checks import ConfigEditRefused

TOKEN = "td5203-literal-Qx7Zp4Lm9Kv2"
PASSWORD = "td5203-url-Hb3Wn8Rs5"

MCP_BLOCK = f"""\
mcp:
  servers:
    docs:
      transport: stdio
      command: ["docs-mcp"]
      env:
        DOCS_TOKEN: "keychain:docs-token"
        PLAIN_TOKEN: "{TOKEN}"
    local:
      transport: http
      url: http://127.0.0.1:9100/mcp
      headers:
        Authorization: "Bearer {TOKEN}"
    plain:
      transport: http
      url: http://127.0.0.1:9300/mcp
    vendor:
      transport: http
      url: https://mcp.vendor-one.example/mcp
      allow_remote: true
      headers:
        Authorization: "keychain:vendor-auth"
"""


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = ensure_user_config(tmp_path / "config.yaml")
    text = path.read_text(encoding="utf-8").replace("mcp:\n  servers: {}\n", MCP_BLOCK, 1)
    path.write_text(text, encoding="utf-8")
    load_config(path)
    return path


def _changed(old: str, new: str) -> list[str]:
    return [
        line
        for line in difflib.ndiff(old.splitlines(), new.splitlines())
        if line.startswith(("- ", "+ "))
    ]


def _refused(path: Path, *args: Any, **kwargs: Any) -> ConfigEditRefused:
    before = path.read_bytes()
    with pytest.raises(ConfigEditRefused) as caught:
        apply_edit(path, *args, **kwargs)
    assert path.read_bytes() == before, "a refused edit changed the file"
    return caught.value


def test_the_view_carries_values_set_paths_and_no_secret(config: Path, tmp_path: Path) -> None:
    text = config.read_text(encoding="utf-8").replace(
        "      base_url: http://127.0.0.1:11434/v1\n",
        f"      base_url: http://user:{PASSWORD}@127.0.0.1:11434/v1\n",
        1,
    )
    config.write_text(text, encoding="utf-8")
    view = build_view(config)
    wire = json.dumps([view.values, view.set_paths])
    assert TOKEN not in wire and PASSWORD not in wire and "docs-token" not in wire
    docs = view.values["mcp"]["servers"]["docs"]
    assert docs["env"] == {"DOCS_TOKEN": "keychain", "PLAIN_TOKEN": "config"}
    assert view.values["mcp"]["servers"]["local"]["headers"] == {"Authorization": "config"}
    assert "user:[REDACTED]@127.0.0.1" in wire
    assert ["workflow", "max_parallel"] in view.set_paths
    assert ["mcp", "servers", "docs", "env", "PLAIN_TOKEN"] in view.set_paths
    assert view.version == version_of(text)
    assert view.values["search"]["max_results"] == 8


def test_a_scalar_round_trips_and_nothing_else_moves(config: Path) -> None:
    before = config.read_text(encoding="utf-8")
    written = apply_edit(config, "set", ["workflow", "max_parallel"], 5)
    assert written is not None and written.workflow.max_parallel == 5
    after = config.read_text(encoding="utf-8")
    assert _changed(before, after) == ["-   max_parallel: 3", "+   max_parallel: 5"]
    assert load_config(config).workflow.max_parallel == 5


def test_setting_what_the_file_already_says_writes_nothing(config: Path) -> None:
    before = config.read_bytes()
    assert apply_edit(config, "set", ["workflow", "max_parallel"], 3) is None
    assert config.read_bytes() == before


def test_enums_lists_and_nested_json(config: Path) -> None:
    apply_edit(config, "set", ["compaction", "summary"], "extractive")
    apply_edit(config, "set", ["consult", "tiers"], ["brain", "worker"])
    apply_edit(config, "set", ["clear_tool_results", "exclude_tools"], ["shell", "fs_read"])
    body = {"chat_template_kwargs": {"enable_thinking": False}}
    apply_edit(config, "set", ["presets", "local", "worker", "extra_body"], body)
    loaded = load_config(config)
    assert loaded.compaction.summary == "extractive"
    assert loaded.consult.tiers == ["brain", "worker"]
    assert loaded.clear_tool_results.exclude_tools == ["shell", "fs_read"]
    assert loaded.presets["local"].worker.extra_body == body
    text = config.read_text(encoding="utf-8")
    assert '  tiers: ["brain", "worker"]\n' in text  # the shipped flow list stays flow
    assert '    - "shell"\n    - "fs_read"\n' in text


@pytest.mark.parametrize(
    ("path", "value", "says"),
    [
        (["workflow", "max_parallel"], 99, "less than or equal to 16"),
        (["workflow", "max_parallel"], "5", "must be a whole number"),
        (["compaction", "summary"], "bogus", "must be one of model, extractive"),
        (["consult", "tiers"], ["boss"], "must be one of brain"),
        (["shell", "read_only", "commands"], ["ls", "rm"], "must be one of"),
        (["presets", "local", "worker", "extra_body"], {"model": "x"}, "cannot set model"),
        (["worktree", "folder"], "relative/dir", "absolute path"),
        (["session", "log_max_events"], None, "needs a value"),
        (["search", "nope"], 1, "no key search.nope"),
        (["search"], {}, "change one key at a time"),
        (["hooks", "after_write"], [], "one entry at a time"),
        (["active_preset"], "nope", "no model by that name"),
        (["presets", "local", "brain", "context_window"], 1000, "too little room"),
    ],
)
def test_an_invalid_value_is_refused_and_the_file_is_untouched(
    config: Path, path: list[Any], value: Any, says: str
) -> None:
    refused = _refused(config, "set", path, value)
    assert says in refused.message, refused.message
    assert refused.code in ("invalid", "not_found", "not_editable")


def test_a_masked_value_is_never_written_back(config: Path) -> None:
    masked = "http://user:[REDACTED]@127.0.0.1:11434/v1"
    refused = _refused(config, "set", ["presets", "local", "brain", "base_url"], masked)
    assert "hidden value" in refused.message


def test_secret_and_read_only_keys_go_to_their_own_pages(config: Path) -> None:
    for path in (
        ["mcp", "servers", "docs", "env"],
        ["mcp", "servers", "docs", "env", "PLAIN_TOKEN"],
        ["mcp", "servers", "local", "headers", "Authorization"],
    ):
        assert _refused(config, "set", path, "x").code == "secret"
        assert _refused(config, "reset", path).code == "secret"
    refused = _refused(config, "set", ["credentials", "openrouter", "name"], "Mine")
    assert refused.code == "not_editable" and "API keys" in refused.message
    added = _refused(config, "set", ["mcp", "servers", "new"], {"transport": "stdio"})
    assert "MCP servers" in added.message
    removed = _refused(config, "reset", ["mcp", "servers", "docs"])
    assert "keychain" in removed.message
    # Written whole, an existing server would lose the references kept in its env.
    whole = {"transport": "stdio", "command": ["docs-mcp"]}
    assert "one key at a time" in _refused(config, "set", ["mcp", "servers", "docs"], whole).message
    assert "Add a model" in _refused(config, "set", ["presets", "x"], {}).message


def test_mcp_fields_are_editable_but_an_off_box_url_needs_the_opt_in(config: Path) -> None:
    apply_edit(config, "set", ["mcp", "servers", "docs", "read_only_tools"], ["search"])
    url = ["mcp", "servers", "plain", "url"]
    refused = _refused(config, "set", url, "http://203.0.113.9/mcp")
    assert "allow_remote" in refused.message
    # A server with no token and no opt-in moves freely.
    apply_edit(config, "set", url, "http://127.0.0.1:9400/mcp")
    assert load_config(config).mcp.servers["plain"].url == "http://127.0.0.1:9400/mcp"


def test_a_server_never_takes_its_tokens_or_opt_in_to_another_address(config: Path) -> None:
    """TD-5161: those were for the old server. The MCP page drops them and asks again."""
    url = ["mcp", "servers", "local", "url"]
    apply_edit(config, "set", url, "http://127.0.0.1:9100/mcp/v2")  # the same server
    moves = [
        (url, "http://127.0.0.1:9200/mcp"),  # another port is another server
        (["mcp", "servers", "vendor", "url"], "https://collector.other-host.example/mcp"),
        (["mcp", "servers", "local", "transport"], "stdio"),
        (["mcp", "servers", "docs", "transport"], "http"),
    ]
    for path, value in moves:
        refused = _refused(config, "set", path, value)
        assert refused.code == "not_editable" and "MCP servers" in refused.message, path
    servers = load_config(config).mcp.servers
    assert servers["local"].url == "http://127.0.0.1:9100/mcp/v2"
    assert servers["vendor"].headers == {"Authorization": "keychain:vendor-auth"}
    # The off-box opt-in alone was for the old host as well.
    apply_edit(config, "set", ["mcp", "servers", "plain", "allow_remote"], True)
    plain = ["mcp", "servers", "plain", "url"]
    assert _refused(config, "set", plain, "https://203.0.113.9/mcp").code == "not_editable"


@pytest.mark.parametrize(
    ("bind", "ok"),
    [
        ("0.0.0.0", False),
        ("::", False),
        ("*", False),
        ("192.168.1.5", False),
        ("100.100.1.2", True),
        ("tailscale0", True),
        ("", True),
    ],
)
def test_the_remote_bind_never_names_what_the_listener_refuses(
    config: Path, bind: str, ok: bool
) -> None:
    if ok:
        apply_edit(config, "set", ["remote", "bind"], bind)
        assert load_config(config).remote.bind == bind
    else:
        assert "Refusing to bind" in _refused(config, "set", ["remote", "bind"], bind).message


def test_reset_puts_a_fixed_key_back_to_the_shipped_value(config: Path) -> None:
    before = config.read_text(encoding="utf-8")
    apply_edit(config, "set", ["search", "max_results"], 3)
    apply_edit(config, "reset", ["search", "max_results"])
    assert config.read_text(encoding="utf-8") == before
    assert shipped_defaults()[path_key(["search", "max_results"])] == 8
    assert shipped_defaults()[path_key(["compaction", "summary"])] == "model"


def test_reset_of_a_key_the_shipped_file_lacks_removes_it(config: Path) -> None:
    before = config.read_text(encoding="utf-8")
    apply_edit(config, "set", ["mcp", "tool_search", "mode"], "always")
    assert load_config(config).mcp.tool_search.mode == "always"
    apply_edit(config, "reset", ["mcp", "tool_search", "mode"])
    assert load_config(config).mcp.tool_search.mode == "auto"
    assert "mode" not in config.read_text(encoding="utf-8").split("tool_search:")[-1][:40]
    # The emptied map stays a map: null would not load as the section.
    assert _changed(before, config.read_text(encoding="utf-8")) == ["+   tool_search: {}"]


def test_reset_inside_an_entry_removes_the_key(config: Path) -> None:
    before = config.read_text(encoding="utf-8")
    apply_edit(config, "set", ["presets", "local", "brain", "vision"], True)
    assert load_config(config).presets["local"].brain.vision
    apply_edit(config, "reset", ["presets", "local", "brain", "vision"])
    assert config.read_text(encoding="utf-8") == before
    refused = _refused(config, "reset", ["presets", "local", "brain", "base_url"])
    assert "no default" in refused.message


def test_list_items_are_added_and_removed_against_the_version_read(config: Path) -> None:
    hook = {"name": "ruff", "command": ["ruff", "format", "{files}"], "match": ["*.py"]}
    version = version_of(config.read_text(encoding="utf-8"))
    apply_edit(config, "set", ["hooks", "after_write", 0], hook, version)
    assert load_config(config).hooks.after_write[0].command == ["ruff", "format", "{files}"]
    stale = _refused(config, "reset", ["hooks", "after_write", 0], version)
    assert stale.code == "stale"
    fresh = version_of(config.read_text(encoding="utf-8"))
    apply_edit(config, "reset", ["hooks", "after_write", 0], version=fresh)
    assert load_config(config).hooks.after_write == []
    unknown = {"command": ["x"], "env": {"A": "b"}}
    fresh = version_of(config.read_text(encoding="utf-8"))
    refused = _refused(config, "set", ["hooks", "after_write", 0], unknown, fresh)
    assert "has no key env" in refused.message


def test_a_nullable_group_turns_on_and_off(config: Path) -> None:
    apply_edit(config, "set", ["presets", "local", "worker", "reply_guard"], {})
    assert load_config(config).presets["local"].worker.reply_guard is not None
    apply_edit(config, "set", ["presets", "local", "worker", "reply_guard", "retries"], 4)
    assert load_config(config).presets["local"].worker.reply_guard.retries == 4  # type: ignore[union-attr]
    apply_edit(config, "reset", ["presets", "local", "worker", "reply_guard"])
    assert load_config(config).presets["local"].worker.reply_guard is None


def test_a_section_the_loader_fills_is_copied_in_before_an_edit(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("presets: {}\n", encoding="utf-8")
    assert load_config(path).compaction.summary == "model"  # filled from the shipped file
    apply_edit(path, "set", ["compaction", "reinject_files"], 2)
    loaded = load_config(path)
    assert loaded.compaction.reinject_files == 2
    assert loaded.compaction.summary == "model"  # the neighbours kept their values
    text = path.read_text(encoding="utf-8")
    assert "# How compaction writes its summary (TD-5128)." in text
    version = version_of(text)
    server = {"name": "Mine", "base_url": "http://127.0.0.1:9999/v1"}
    apply_edit(path, "set", ["local_servers", 4], server, version)
    names = [s.name for s in load_config(path).local_servers]
    assert names == ["Ollama", "LM Studio", "vLLM", "llama.cpp", "Mine"]


def test_reset_of_a_key_in_a_filled_section_is_already_done(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("presets: {}\n", encoding="utf-8")
    assert apply_edit(path, "reset", ["compaction", "summary"]) is None
    assert path.read_text(encoding="utf-8") == "presets: {}\n"


def test_the_active_model_is_picked_from_the_catalog(config: Path) -> None:
    apply_edit(config, "set", ["active_preset"], "budget")
    assert load_config(config).active_preset == "budget"
    assert _refused(config, "reset", ["active_preset"]).code == "not_editable"


def test_comments_survive_a_run_of_edits(config: Path) -> None:
    before = config.read_text(encoding="utf-8")
    comments = [line for line in before.splitlines() if line.lstrip().startswith("#")]
    apply_edit(config, "set", ["notify", "slack", "enabled"], True)
    apply_edit(config, "set", ["notify", "slack", "host"], "hooks.slack.com")
    apply_edit(config, "set", ["lsp", "max_diagnostics"], 40)
    apply_edit(config, "set", ["embeddings", "command"], ["embed-server", "--port", "8080"])
    after = config.read_text(encoding="utf-8")
    assert [line for line in after.splitlines() if line.lstrip().startswith("#")] == comments
    assert yaml.safe_load(after)["embeddings"]["command"] == ["embed-server", "--port", "8080"]
    assert _changed(before, after) == [
        "+   command:",
        '+     - "embed-server"',
        '+     - "--port"',
        '+     - "8080"',
        "+   max_diagnostics: 40",
        "-     enabled: false",
        "+     enabled: true",
        '-     host: ""',
        '+     host: "hooks.slack.com"',
    ]
