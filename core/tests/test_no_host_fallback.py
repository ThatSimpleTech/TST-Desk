"""A remote model without `credential` is sent to its own base_url (DOCS2-05).

An unbound remote tier was bound to the shipped `openrouter` key, and the
key's host then replaced the tier's URL, so a keyless company gateway's
prompts went to OpenRouter with the person's OpenRouter key.
"""

from __future__ import annotations

from pathlib import Path

from tstd.config import TierConfig, default_config_yaml, resolve_base_url, resolve_credential_id
from tstd.config_write import check_config_text


def _tier(url: str) -> TierConfig:
    return TierConfig(
        slug="corp/model",
        base_url=url,
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=1000,
        max_output_tokens=100,
    )


def test_a_gateway_keeps_its_url_and_gets_no_key(tmp_path: Path) -> None:
    config = check_config_text(default_config_yaml(), tmp_path / "config.yaml")
    gateway = _tier("https://llm.corp.example/v1")
    assert resolve_credential_id(gateway, config) is None
    assert resolve_base_url(config, gateway) == "https://llm.corp.example/v1"


def test_openrouter_itself_is_unchanged(tmp_path: Path) -> None:
    config = check_config_text(default_config_yaml(), tmp_path / "config.yaml")
    tier = _tier("https://openrouter.ai/api/v1")
    assert resolve_credential_id(tier, config) == "openrouter"
    assert resolve_base_url(config, tier) == "https://openrouter.ai/api/v1"


def test_a_bound_key_still_wins(tmp_path: Path) -> None:
    config = check_config_text(default_config_yaml(), tmp_path / "config.yaml")
    tier = _tier("https://llm.corp.example/v1").model_copy(update={"credential": "corp"})
    assert resolve_credential_id(tier, config) == "corp"
