"""TD-5203: what Settings → Configuration refuses to write, and what it never says.

Every refused edit here leaves config.yaml byte-identical. Covered: a whole
section or map of entries is never reset or created through one key, a key,
token or URL password is never written into the file and never quoted back,
a character that cannot be saved is refused, a feature names only a saved
API key, and the file has a size cap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tstd.config import (
    ConfigError,
    GroundingConfig,
    LocalServerConfig,
    TierConfig,
    ensure_user_config,
    load_config,
)
from tstd.config_editor import MAX_FILE_CHARS, apply_edit, build_view, version_of
from tstd.config_editor_checks import ConfigEditRefused

KEY = "sk-or-v1-" + "0123456789abcdef" * 4
PASSWORD = "hunter2-secret"


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = ensure_user_config(tmp_path / "config.yaml")
    load_config(path)
    return path


def _refused(path: Path, *args: Any, **kwargs: Any) -> ConfigEditRefused:
    before = path.read_bytes()
    with pytest.raises(ConfigEditRefused) as caught:
        apply_edit(path, *args, **kwargs)
    assert path.read_bytes() == before, "a refused edit changed the file"
    return caught.value


def _version(path: Path) -> str:
    return version_of(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "path",
    [
        ["presets"],
        ["mcp"],
        ["mcp", "servers"],
        ["search"],
        ["presets", "local", "brain"],
        ["presets", "local", "brain", "fallbacks"],
    ],
)
def test_a_section_or_a_map_of_entries_is_never_reset_whole(config: Path, path: list[Any]) -> None:
    assert _refused(config, "reset", path).code == "not_editable"


def test_a_key_of_an_entry_this_page_cannot_add_does_not_create_the_entry(config: Path) -> None:
    refused = _refused(config, "set", ["mcp", "servers", "added", "transport"], "http")
    assert "Add servers in Settings → MCP servers" in refused.message
    assert "Add a model" in _refused(config, "set", ["presets", "new", "label"], "New").message


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["presets", "local", "brain", "extra_body"], {"api_key": KEY}),
        (["presets", "local", "brain", "extra_body"], {KEY: True}),
        (["hooks", "session_start", 0], {"command": f"curl -H 'Authorization: Bearer {KEY}' x"}),
        (["presets", "local", "worker", "base_url"], f"https://{KEY}@127.0.0.1:11434/v1"),
        (["presets", "local", "worker", "base_url"], f"http://me:{PASSWORD}@127.0.0.1:11434/v1"),
        (["local_servers", 0, "base_url"], f"http://me:{PASSWORD}@203.0.113.9:8080/v1"),
        (["compaction", "summary_prompt"], f"Summarise the session. Key: {KEY}"),
    ],
)
def test_a_key_or_password_is_never_written_or_quoted_back(
    config: Path, path: list[Any], value: Any
) -> None:
    refused = _refused(config, "set", path, value, _version(config))
    assert refused.code == "secret"
    assert "keychain" in refused.message
    assert KEY not in refused.message and PASSWORD not in refused.message


def test_a_refusal_masks_a_url_password_whatever_raised_it() -> None:
    refused = ConfigEditRefused("invalid", f"bad http://me:{PASSWORD}@203.0.113.9/v1 and {KEY}")
    assert PASSWORD not in refused.message and KEY not in refused.message
    assert "http://me:[REDACTED]@203.0.113.9/v1" in refused.message


@pytest.mark.parametrize(
    "build",
    [
        lambda url: LocalServerConfig(name="X", base_url=url),
        lambda url: TierConfig.model_validate(
            {
                "base_url": url,
                "input_price": 0,
                "output_price": 0,
                "cache_read_price": 0,
                "context_window": 32768,
                "max_output_tokens": 4096,
            }
        ),
        lambda url: GroundingConfig(base_url=url),
    ],
)
def test_a_validator_names_no_url_password(build: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        build(f"http://me:{PASSWORD}@203.0.113.9:8080/v1")
    reasons = [err["msg"] for err in caught.value.errors(include_input=False)]
    assert reasons and all(PASSWORD not in reason for reason in reasons), reasons


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["compaction", "summary_prompt"], "a\ud800b"),
        (["presets", "local", "worker", "extra_body"], {"seed\udc00": 1}),
        (["clear_tool_results", "exclude_tools"], ["shell", "\ud83d"]),
    ],
)
def test_a_character_that_cannot_be_saved_is_refused(
    config: Path, path: list[Any], value: Any
) -> None:
    refused = _refused(config, "set", path, value)
    assert refused.code == "invalid" and "cannot be saved" in refused.message


@pytest.mark.parametrize(
    ("path", "name"),
    [
        (["voice", "credential"], "smtp-password"),
        (["voice", "credential"], "undeclared-x"),
        (["voice", "credential"], "mcp.docs.token"),
        (["judgments", "typesafe_credential"], "github-oauth"),
        (["judgments", "typesafe_credential"], "undeclared-x"),
        (["speech", "credential"], "undeclared-x"),
    ],
)
def test_a_feature_names_only_a_saved_api_key(config: Path, path: list[Any], name: str) -> None:
    assert _refused(config, "set", path, name).code == "invalid"


def test_a_saved_key_and_the_packaged_name_are_accepted(config: Path) -> None:
    apply_edit(config, "set", ["voice", "credential"], "openrouter")
    apply_edit(config, "set", ["judgments", "typesafe_credential"], "openrouter")
    apply_edit(config, "reset", ["judgments", "typesafe_credential"])
    loaded = load_config(config)
    assert loaded.voice.credential == "openrouter"
    assert loaded.judgments.typesafe_credential == "typesafe"


def test_a_hand_edit_naming_another_keychain_item_fails_the_load(config: Path) -> None:
    text = config.read_text(encoding="utf-8").replace(
        'voice:\n  base_url: ""\n  credential: ""\n',
        'voice:\n  base_url: ""\n  credential: "smtp-password"\n',
        1,
    )
    assert "smtp-password" in text
    config.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="not an API key"):
        load_config(config)


def test_the_file_never_grows_past_its_cap(config: Path) -> None:
    refused = _refused(config, "set", ["compaction", "summary_prompt"], "x" * (MAX_FILE_CHARS + 1))
    assert "1 MiB" in refused.message


def test_the_view_masks_a_token_user_name_and_a_password_holding_an_at(config: Path) -> None:
    line = "      base_url: http://127.0.0.1:11434/v1\n"
    text = config.read_text(encoding="utf-8")
    assert text.count(line) >= 2
    text = text.replace(
        line, "      base_url: http://abcd1234efgh5678token@127.0.0.1:11434/v1\n", 1
    )
    text = text.replace(line, "      base_url: http://user:p@ss-w0rd@127.0.0.1:11434/v1\n", 1)
    config.write_text(text, encoding="utf-8")
    wire = json.dumps(build_view(config).values)
    assert "abcd1234efgh5678token" not in wire and "ss-w0rd" not in wire
    assert "http://[REDACTED]@127.0.0.1:11434/v1" in wire
    assert "http://user:[REDACTED]@127.0.0.1:11434/v1" in wire
