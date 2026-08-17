"""Model configuration schema.

Slugs, prices, and provider URLs live in ``config.yaml`` — never in Python
source code (TD-302). This module loads, validates, and selects presets.

A shipped default config ships with the package; on first load it is copied
to the user data directory so users can edit it. Validation produces
actionable error messages that name the offending key.
"""

from __future__ import annotations

import shutil
from functools import lru_cache
from importlib import resources
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

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


class ModelDiscoveryError(Exception):
    """A tier's model could not be resolved from its endpoint (TD-1805).

    Deliberately *not* a :class:`ConfigError`: a config error means the file
    is wrong and editing it is the fix, while this means the file is right
    and the machine is not ready — the same message may succeed once the
    model server is up.  ``endpoint`` and ``fix`` are carried as attributes
    so a caller can render them in its own shape (a doctor row, a wizard
    detail) instead of re-deriving them from the text.
    """

    def __init__(self, message: str, *, endpoint: str, fix: str) -> None:
        super().__init__(f"{message}. {fix}")
        self.endpoint = endpoint
        self.fix = fix


class TierConfig(BaseModel):
    """Configuration for one model tier.

    ``slug`` is optional, and only for a loopback endpoint: a local server's
    model tag belongs to the machine, not to the shipped defaults, so it is
    discovered from ``/v1/models`` on first use (TD-1805).  Omitted and
    ``null`` mean the same thing — a bare ``slug:`` in YAML *is* ``null``, so
    letting them diverge would make whitespace meaningful.  An empty string
    is a validation error rather than a third spelling of "unset": it is a
    half-finished edit, never a statement of intent.
    """

    slug: Annotated[str, Field(min_length=1)] | None = None
    base_url: str = Field(min_length=1)
    input_price: float = Field(ge=0)
    output_price: float = Field(ge=0)
    cache_read_price: float = Field(ge=0)
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def _slug_required_off_box(self) -> TierConfig:
        """An off-box tier must name its model (TD-1805).

        Discovery is loopback-only, so a remote tier with no slug can never
        be filled in later — it stays a config error, caught at load rather
        than as a null ``model`` on the wire.
        """
        if self.slug is None and not is_loopback_url(self.base_url):
            raise ValueError(
                f"slug is required for the off-box endpoint {self.base_url}; "
                "only a loopback endpoint discovers its model from /v1/models"
            )
        return self

    def require_slug(self) -> str:
        """The model slug, narrowed to ``str``.

        Unset here means discovery never ran, which is a bug in the call
        path rather than a user's mistake — raising keeps it loud instead of
        sending ``"model": null`` to a provider and reading the reply.
        """
        if self.slug is None:
            raise ModelDiscoveryError(
                f"no model has been resolved for {self.base_url}",
                endpoint=self.base_url,
                fix="Resolve the tier's slug before using it (tstd.discovery).",
            )
        return self.slug


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
