"""Model configuration schema.

Slugs, prices, and provider URLs live in ``config.yaml`` — never in Python
source code (TD-302). This module loads, validates, and selects presets.

A shipped default config ships with the package; on first load it is copied
to the user data directory so users can edit it. Validation produces
actionable error messages that name the offending key.
"""

from __future__ import annotations

import re
import shutil
from functools import lru_cache
from importlib import resources
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .logging import user_data_dir

TierName = Literal["brain", "worker", "validator"]
TIER_NAMES: tuple[TierName, ...] = ("brain", "worker", "validator")

# Presets shipped with the package. Users may add more.
PRESETS: tuple[str, ...] = ("tst-default", "budget", "local", "vllm")

DEFAULT_PRESET = "tst-default"

# Implicit keychain account for an unbound remote tier (TD-1717).
DEFAULT_CREDENTIAL_ID = "openrouter"
RESERVED_CREDENTIAL_IDS = frozenset({"slack-webhook", "ntfy-topic"})
CREDENTIAL_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# A second OpenRouter key slugifies to openrouter-2 (TD-1718). Same host.
_OPENROUTER_FAMILY_RE = re.compile(r"^openrouter(?:-\d+)?$")

_DEFAULT_CONFIG_RESOURCE = "config.yaml"


def slugify_credential_name(name: str) -> str:
    """Turn a display name into a keychain-safe credential id."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:32]
    if not slug or not slug[0].isalpha():
        slug = ("key-" + slug).strip("-")[:32]
    if not slug or not slug[0].isalpha():
        slug = "key"
    return slug


def allocate_credential_id(name: str, existing: set[str]) -> str:
    """Pick an unused id for *name*, skipping reserved keychain accounts."""
    base = slugify_credential_name(name)
    candidates = [base, *[f"{base}-{n}" for n in range(2, 100)]]
    for candidate in candidates:
        trimmed = candidate[:32]
        if (
            CREDENTIAL_ID_RE.match(trimmed)
            and trimmed not in existing
            and trimmed not in RESERVED_CREDENTIAL_IDS
        ):
            return trimmed
    raise ConfigError(f"Could not allocate a credential id for {name!r}")


def resolve_credential_id(tier: TierConfig) -> str | None:
    """Keychain account a tier will send, or None when it sends no key.

    A bound id always wins, including on loopback (a local server can
    require ``--api-key``). Unbound loopback stays keyless (TD-1801).
    Unbound remote keeps the historical ``openrouter`` account.
    """
    if tier.credential:
        return tier.credential
    if is_loopback_url(tier.base_url):
        return None
    return DEFAULT_CREDENTIAL_ID


def is_openrouter_family(credential_id: str) -> bool:
    """True for ``openrouter`` and ``openrouter-<n>`` (TD-1718)."""
    return _OPENROUTER_FAMILY_RE.fullmatch(credential_id) is not None


def credential_base_url(config: ModelConfig, credential_id: str | None) -> str | None:
    """Host this named key talks to, or None to keep the tier URL.

    A catalog row with ``base_url`` wins. ``openrouter`` / ``openrouter-2``
    without one inherit the shipped OpenRouter host so a second key named
    OPENROUTER still leaves the machine (TD-1718). A keyed local server
    omits ``base_url`` and stays on the tier.
    """
    if not credential_id:
        return None
    cred = config.credentials.get(credential_id)
    if cred is not None and cred.base_url:
        return cred.base_url
    if is_openrouter_family(credential_id):
        default = config.credentials.get(DEFAULT_CREDENTIAL_ID)
        if default is not None and default.base_url:
            return default.base_url
    return None


def resolve_base_url(config: ModelConfig, tier: TierConfig) -> str:
    """Endpoint a turn actually calls (TD-1718).

    Bound (or implicit) credential host first; otherwise the tier URL.
    """
    return credential_base_url(config, resolve_credential_id(tier)) or tier.base_url


def apply_credential_host(config: ModelConfig, tier: TierConfig) -> TierConfig:
    """Return *tier* with the credential host applied when it differs."""
    url = resolve_base_url(config, tier)
    if url == tier.base_url:
        return tier
    return tier.model_copy(update={"base_url": url})


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
    # Named key from the credentials catalog (TD-1717). None / omitted /
    # blank means unbound: loopback sends no key, remote uses openrouter.
    credential: str | None = None

    @field_validator("credential")
    @classmethod
    def _blank_credential_is_unbound(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

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


class CredentialConfig(BaseModel):
    """Display name and optional host for one keychain-backed API key.

    The secret is never here. The mapping key is the id. ``name`` is what
    Settings shows. ``base_url`` is the host this key talks to (TD-1718):
    a bound tier uses it instead of the preset URL, so picking OpenRouter
    on a local preset does not send an OpenRouter slug to localhost.
    Omit it for a keyed local server — the tier URL stays in charge.
    """

    name: str = Field(min_length=1, max_length=40)
    base_url: str | None = None

    @field_validator("base_url")
    @classmethod
    def _blank_base_url_is_unset(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().rstrip("/")
        return cleaned or None

    @model_validator(mode="after")
    def _base_url_is_http(self) -> CredentialConfig:
        if self.base_url is None:
            return self
        try:
            parts = urlsplit(self.base_url)
        except ValueError as e:
            raise ValueError(f"credential base_url is not a URL: {e}") from e
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("credential base_url must be an http(s) OpenAI-compatible endpoint")
        return self


class SearchConfig(BaseModel):
    """Web search and page fetch (TD-609, TD-610).

    ``base_url`` is the only host ``web_search`` may reach. ``web_fetch``
    takes a URL the user approved. Empty ``base_url`` disables search only.
    """

    base_url: str = ""
    timeout_seconds: float = Field(default=15.0, gt=0)
    max_results: int = Field(default=8, ge=1, le=20)
    fetch_max_bytes: int = Field(default=200_000, ge=1)


class ProjectContextConfig(BaseModel):
    """Pinned-file budget on the brain prompt (TD-2805)."""

    token_budget: int = Field(default=2000, ge=1)


DEFAULT_LOG_MAX_EVENTS = 10000


class ProviderRetryConfig(BaseModel):
    """How long to keep trying a retryable provider failure (429, 5xx).

    The shipped default spends four attempts inside roughly eight seconds,
    which covers a blip and nothing longer. A gateway serving a shared
    upstream pool answers "temporarily rate-limited upstream, please retry
    shortly" and sends no ``Retry-After``, so eight seconds of patience ends
    the turn while the capacity it needed was still a minute out. The cost of
    raising this is turn latency on a provider that is genuinely down; the
    cost of leaving it low is a turn that fails for want of waiting.

    ``max_delay`` caps a single wait; ``max_retries`` caps how many there
    are. Attempts back off exponentially from ``initial_delay``, so 8 retries
    is roughly three minutes of persistence, not eight seconds.
    """

    max_retries: int = Field(default=3, ge=0, le=20)
    initial_delay: float = Field(default=1.0, gt=0)
    max_delay: float = Field(default=60.0, gt=0)


class SessionConfig(BaseModel):
    """On-disk session event-log window (TD-2901).

    Count of events, not bytes: attach is ``from_seq``, and a byte cap
    would drop a different prefix than the seq cursor. Zero and omitted
    must not mean unbounded — the default is ``DEFAULT_LOG_MAX_EVENTS``.
    """

    log_max_events: int = Field(default=DEFAULT_LOG_MAX_EVENTS, ge=1)


class EmbeddingsConfig(BaseModel):
    """Local embeddings sidecar (TD-2202, TD-2204).

    ``base_url`` is the only host the embeddings client may reach. Empty
    disables. The client speaks OpenAI ``POST /v1/embeddings``, never
    Ollama's native embed route.

    ``command`` is host-only. Empty or omitted means attach-only: the
    client POSTs to ``base_url`` if something is already listening. A
    filled ``base_url`` never causes a spawn.
    """

    base_url: str = ""
    model: str = ""
    timeout_seconds: float = Field(default=2.0, gt=0)
    top_k: int = Field(default=4, ge=1)
    token_budget: int = Field(default=2000, ge=1)
    command: str | list[str] = ""

    @field_validator("command", mode="before")
    @classmethod
    def _coerce_command(cls, value: Any) -> str | list[str]:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return [str(item) for item in value]
        raise ValueError("command must be a string or a list of arguments")


class SlackNotifyConfig(BaseModel):
    """Slack incoming webhook (TD-3801). Off by default.

    ``host`` is the only host ``tstd.notify.slack.send`` may reach. The
    webhook URL itself is a keychain secret (account ``tst-slack-webhook``),
    never this file, never a log, never the audit database. Empty ``host``
    or ``enabled: false`` means no send.
    """

    enabled: bool = False
    host: str = ""
    timeout_seconds: float = Field(default=5.0, gt=0)


class NtfyNotifyConfig(BaseModel):
    """ntfy topic POST (TD-3802). Off by default.

    ``host`` is the only host ``tstd.notify.ntfy.send`` may reach. The
    topic URL itself is a keychain secret (account ``tst-ntfy-topic``),
    never this file, never a log, never the audit database. Empty ``host``
    or ``enabled: false`` means no send. Discord/Telegram are TD-4707.
    """

    enabled: bool = False
    host: str = ""
    timeout_seconds: float = Field(default=5.0, gt=0)


class NotifyConfig(BaseModel):
    """Outbound notification channels. Slack first, ntfy optional; no gateway."""

    slack: SlackNotifyConfig = Field(default_factory=SlackNotifyConfig)
    ntfy: NtfyNotifyConfig = Field(default_factory=NtfyNotifyConfig)


class GroundingConfig(BaseModel):
    """Local click-grounding model (TD-3902).

    Empty ``base_url`` is off: desktop clicks use the intended (x, y)
    (TD-3304). A filled URL must be loopback — off-box is a load error,
    not a silent remote call. Optional ``slug`` is discovered from
    ``/v1/models`` the same way a loopback tier is (TD-1805).
    """

    base_url: str = ""
    slug: Annotated[str, Field(min_length=1)] | None = None
    timeout_seconds: float = Field(default=8.0, gt=0)

    @field_validator("base_url")
    @classmethod
    def _strip_base_url(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _loopback_only(self) -> GroundingConfig:
        if self.base_url and not is_loopback_url(self.base_url):
            raise ValueError(
                f"computer_use.grounding.base_url must be a loopback endpoint "
                f"(got {self.base_url}); off-box grounding is not allowed"
            )
        return self


class ComputerUseConfig(BaseModel):
    """Desktop computer-use sidecar (TD-3301) and browser (TD-1710).

    Empty ``command`` is mock-only: no child, no real pointer. A non-empty
    value is argv for ``mcp/tst-cu-mcp`` over stdio. The daemon owns the
    child. No socket.

    ``browser`` is ``mock`` (default, CI) or ``playwright``. Playwright
    missing always falls back to the mock. The live profile lives under
    the user data dir, not the workspace.

    ``grounding`` is an optional local vision model for click targeting
    (TD-3902). Empty ``grounding.base_url`` leaves the TD-3304 path.

    ``local_worker_preset`` names the preset whose *worker* tier is used
    for the worker client after a session has used a desktop_ or
    browser_ tool (TD-3903). Default ``vllm``. Empty never remaps.
    Brain stays on the active preset.
    """

    command: str | list[str] = ""
    browser: Literal["mock", "playwright"] = "mock"
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    local_worker_preset: str = "vllm"

    @field_validator("local_worker_preset", mode="before")
    @classmethod
    def _strip_local_worker_preset(cls, value: object) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("local_worker_preset must be a string")
        return value.strip()

    @field_validator("command", mode="before")
    @classmethod
    def _coerce_command(cls, value: Any) -> str | list[str]:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return [str(item) for item in value]
        raise ValueError("command must be a string or a list of arguments")


class RemoteConfig(BaseModel):
    """Opt-in Tailscale bind (TD-3601). Empty is off — loopback only."""

    bind: str = ""

    @field_validator("bind")
    @classmethod
    def _strip_bind(cls, value: str) -> str:
        return value.strip()


class AutonomyConfig(BaseModel):
    """Rootless container used only for autonomous runs (TD-4301).

    Interactive sessions ignore this block. ``runtime`` is the argv0
    probed on PATH (or an absolute path). ``image`` is configuration,
    never a Python literal — same rule as model slugs.
    """

    model_config = ConfigDict(extra="forbid")

    runtime: str = Field(default="podman", min_length=1)
    image: str = Field(default="docker.io/library/alpine:3.21", min_length=1)

    @field_validator("runtime", "image")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class ModelConfig(BaseModel):
    """Top-level model configuration loaded from config.yaml."""

    presets: dict[str, Preset]
    active_preset: str = DEFAULT_PRESET
    credentials: dict[str, CredentialConfig] = Field(default_factory=dict)
    search: SearchConfig = Field(default_factory=SearchConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    project_context: ProjectContextConfig = Field(default_factory=ProjectContextConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    provider_retry: ProviderRetryConfig = Field(default_factory=ProviderRetryConfig)
    computer_use: ComputerUseConfig = Field(default_factory=ComputerUseConfig)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)

    @field_validator("credentials")
    @classmethod
    def _valid_credential_ids(
        cls, value: dict[str, CredentialConfig]
    ) -> dict[str, CredentialConfig]:
        reserved = sorted(RESERVED_CREDENTIAL_IDS & set(value))
        if reserved:
            raise ValueError(
                f"credential id(s) reserved for other keychain accounts: {', '.join(reserved)}"
            )
        bad = sorted(cid for cid in value if not CREDENTIAL_ID_RE.match(cid))
        if bad:
            raise ValueError(
                f"credential id(s) must be a lowercase slug "
                f"[a-z][a-z0-9-]{{0,31}}: {', '.join(bad)}"
            )
        return value

    @model_validator(mode="after")
    def _bound_credentials_exist(self) -> ModelConfig:
        known = set(self.credentials) | {DEFAULT_CREDENTIAL_ID}
        for preset_name, preset in self.presets.items():
            for tier_name in TIER_NAMES:
                tier = getattr(preset, tier_name)
                cid = tier.credential
                if cid is not None and cid not in known:
                    raise ValueError(
                        f"presets.{preset_name}.{tier_name}.credential "
                        f"{cid!r} is not a declared credential"
                    )
        return self

    def tier(self, name: TierName) -> TierConfig:
        """Get the tier config for the active preset."""
        return self.tiers()[name]

    def tiers(self) -> dict[TierName, TierConfig]:
        """Get all tier configs for the active preset."""
        preset = self.presets[self.active_preset]
        return {name: preset.__getattribute__(name) for name in TIER_NAMES}

    def requires_api_key(self) -> bool:
        """Whether the active preset will send at least one key (TD-1801, TD-1717).

        True when any tier is off-box (unbound remotes still use
        ``openrouter``) or a loopback tier is bound to a named key.
        Conservative on purpose: a mixed preset never degrades into an
        unauthenticated remote call.
        """
        return any(resolve_credential_id(t) is not None for t in self.tiers().values())


class ConfigError(Exception):
    """Raised when the model configuration is invalid or missing."""


def _merge_shipped_credential_hosts(data: dict[str, Any], shipped: dict[str, Any]) -> None:
    """Fill missing credential hosts from the shipped catalog (TD-1718).

    A user ``credentials:`` block is not replaced — that would drop their
    names. Missing shipped ids are added; a shipped id with no ``base_url``
    inherits the shipped host so a rename of OpenRouter keeps the endpoint.
    """
    shipped_creds = shipped.get("credentials")
    if not isinstance(shipped_creds, dict):
        return
    user_creds = data.get("credentials")
    if not isinstance(user_creds, dict):
        data["credentials"] = {
            cid: dict(row) if isinstance(row, dict) else row for cid, row in shipped_creds.items()
        }
        return
    for cid, row in shipped_creds.items():
        if not isinstance(row, dict):
            continue
        existing = user_creds.get(cid)
        if not isinstance(existing, dict):
            user_creds[cid] = dict(row)
            continue
        if not str(existing.get("base_url") or "").strip() and row.get("base_url"):
            existing["base_url"] = row["base_url"]


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
    # A user copy from before search / embeddings existed has no key.
    # Fill from the shipped file so the destination exists without
    # rewriting theirs.
    shipped: dict[str, Any] | None = None

    def _shipped() -> dict[str, Any]:
        nonlocal shipped
        if shipped is None:
            loaded = yaml.safe_load(default_config_yaml())
            shipped = loaded if isinstance(loaded, dict) else {}
        return shipped

    for key in (
        "search",
        "embeddings",
        "project_context",
        "session",
        "computer_use",
        "remote",
        "notify",
        "autonomy",
        "credentials",
    ):
        if key in data:
            continue
        value = _shipped().get(key)
        if isinstance(value, dict):
            data[key] = value

    _merge_shipped_credential_hosts(data, _shipped())

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
