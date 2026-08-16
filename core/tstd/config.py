"""Model configuration schema.

Slugs, prices, and provider URLs live in ``config.yaml`` — never in Python
source code (TD-302). This module loads, validates, and selects presets.

A shipped default config ships with the package; on first load it is copied
to the user data directory so users can edit it. Validation produces
actionable error messages that name the offending key.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from functools import lru_cache
from importlib import resources
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, ValidationError

from .logging import user_data_dir

TierName = Literal["brain", "worker", "validator"]
TIER_NAMES: tuple[TierName, ...] = ("brain", "worker", "validator")

# Presets shipped with the package. Users may add more.
PRESETS: tuple[str, ...] = ("tst-default", "budget", "local")

DEFAULT_PRESET = "tst-default"

_DEFAULT_CONFIG_RESOURCE = "config.yaml"


def is_loopback_url(url: str) -> bool:
    """True when *url* points at this machine (127.0.0.0/8, ``::1``, ``localhost``).

    A loopback endpoint is on-box by construction, so there is no third party
    to authenticate against and no credential to send (TD-1801). Anything we
    cannot confidently classify — no scheme, an unparseable host, a name that
    merely looks local — is treated as remote, so an ambiguous URL keeps the
    key requirement rather than silently dropping it.

    Deliberately separate from ``ws.validate_interface``: that guards which
    interface we *bind* (§2.1), this classifies an endpoint we *call*.
    """
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class TierConfig(BaseModel):
    """Configuration for one model tier."""

    slug: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    input_price: float = Field(ge=0)
    output_price: float = Field(ge=0)
    cache_read_price: float = Field(ge=0)
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)


class Preset(BaseModel):
    """A complete set of tier configurations."""

    brain: TierConfig
    worker: TierConfig
    validator: TierConfig


class ModelConfig(BaseModel):
    """Top-level model configuration loaded from config.yaml."""

    presets: dict[str, Preset]
    active_preset: str = DEFAULT_PRESET

    def tier(self, name: TierName) -> TierConfig:
        """Get the tier config for the active preset."""
        return self.tiers()[name]

    def tiers(self) -> dict[TierName, TierConfig]:
        """Get all tier configs for the active preset."""
        preset = self.presets[self.active_preset]
        return {name: preset.__getattribute__(name) for name in TIER_NAMES}

    def requires_api_key(self) -> bool:
        """Whether the active preset needs a stored key (TD-1801).

        False only when every tier is a loopback endpoint. Conservative on
        purpose: one off-box tier means the workspace still needs a key, so a
        mixed preset never degrades into an unauthenticated remote call.
        """
        return not all(is_loopback_url(t.base_url) for t in self.tiers().values())


class ConfigError(Exception):
    """Raised when the model configuration is invalid or missing."""


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load and parse a YAML file, raising ConfigError on failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"Failed to read config file {path}: {e}") from e

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a mapping at the top level")

    return data


def default_config_yaml() -> str:
    """Return the shipped default config.yaml as a string."""
    return resources.files("tstd").joinpath(_DEFAULT_CONFIG_RESOURCE).read_text(encoding="utf-8")


def ensure_user_config(path: Path | None = None) -> Path:
    """Copy the shipped default config to the user data dir if missing.

    Returns the path to the user config file.
    """
    config_path = path or (user_data_dir() / "config.yaml")
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfileobj(
            resources.files("tstd").joinpath(_DEFAULT_CONFIG_RESOURCE).open("rb"),
            config_path.open("wb"),
        )
    return config_path


def save_active_preset(name: str, path: Path | None = None) -> Path:
    """Persist ``active_preset: <name>`` in the user config (TD-1101).

    The shipped config is comment-heavy and survives a PyYAML round-trip
    poorly, so instead of dumping we surgically rewrite the single top-level
    ``active_preset:`` line — appending it when absent.  The write is atomic
    (same-directory temp file + ``os.replace``), so a crash mid-write never
    leaves a torn config.

    Returns the path written.  Raises ``ConfigError`` if the name is not a
    declared preset of the loaded config.
    """
    config_path = ensure_user_config(path)
    config = load_config(config_path)
    if name not in config.presets:
        raise ConfigError(
            f"Unknown preset {name!r}; declared presets: {', '.join(sorted(config.presets))}"
        )

    text = config_path.read_text(encoding="utf-8")
    new_line = f"active_preset: {name}"
    pattern = re.compile(r"^active_preset:.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(new_line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n\n" + new_line + "\n"

    fd, tmp_name = tempfile.mkstemp(dir=config_path.parent, prefix=config_path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, config_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return config_path


def load_config(path: Path | None = None) -> ModelConfig:
    """Load and validate the model configuration.

    Args:
        path: Explicit config path. Defaults to the user config file,
            which is created from the shipped default if missing.

    Returns:
        The validated ``ModelConfig``.

    Raises:
        ConfigError: If the file is missing, invalid YAML, or fails
            validation. Messages name the offending key.
    """
    config_path = ensure_user_config(path)
    data = _load_yaml(config_path)

    try:
        config = ModelConfig.model_validate(data)
    except ValidationError as e:
        # Convert pydantic errors into actionable messages naming the key.
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        raise ConfigError(f"Invalid model configuration in {config_path}: {details}") from e

    if config.active_preset not in config.presets:
        raise ConfigError(
            f"active_preset '{config.active_preset}' not found in presets {sorted(config.presets)}"
        )

    return config


@lru_cache(maxsize=1)
def cached_config(path: Path | None = None) -> ModelConfig:
    """Load config once per process; invalidate with ``cached_config.cache_clear()``."""
    return load_config(path)
