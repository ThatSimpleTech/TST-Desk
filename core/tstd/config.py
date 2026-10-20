"""Model configuration schema.

Slugs, prices, and provider URLs live in ``config.yaml`` — never in Python
source code (TD-302). This module loads, validates, and selects presets.

A shipped default config ships with the package; on first load it is copied
to the resolved config path so users can edit it. With no path that is the
user data directory. A daemon passes its data directory, so ``--data-dir``
is the file it reads and writes. Validation produces actionable error
messages that name the offending key.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from functools import lru_cache
from importlib import resources
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .logging import get_logger, user_data_dir
from .mcp_values import coerce_map
from .provider import TimeoutConfig
from .shell_read_only import READ_ONLY_ENTRIES

TierName = Literal["brain", "worker", "validator"]
TIER_NAMES: tuple[TierName, ...] = ("brain", "worker", "validator")

# Presets shipped with the package. Users may add more.
PRESETS: tuple[str, ...] = ("tst-default", "budget", "local", "vllm")

DEFAULT_PRESET = "tst-default"

# Implicit keychain account for an unbound remote tier (TD-1717).
DEFAULT_CREDENTIAL_ID = "openrouter"
RESERVED_CREDENTIAL_IDS = frozenset(
    {
        "slack-webhook",
        "ntfy-topic",
        "discord-webhook",
        "telegram-bot",
        "smtp-password",
        "github-oauth",
        "microsoft-oauth",
        "google-oauth",
        "google-client-secret",
    }
)
CREDENTIAL_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# A second OpenRouter key slugifies to openrouter-2 (TD-1718). Same host.
_OPENROUTER_FAMILY_RE = re.compile(r"^openrouter(?:-\d+)?$")

# Request fields a tier's ``extra_body`` may not set (TD-5008): the loop
# owns them. ``max_completion_tokens`` is the newer OpenAI name for the
# output cap, so it would fight ``max_tokens``.
EXTRA_BODY_RESERVED_KEYS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "stream",
        "stream_options",
        "max_tokens",
        "max_completion_tokens",
    }
)

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
    # When true, PNG/JPEG/GIF/WebP attachments are accepted on user turns
    # routed to this tier (TD-4705). Default false — capability is config,
    # never inferred from a slug in code.
    vision: bool = False
    # Named key from the credentials catalog (TD-1717). None / omitted /
    # blank means unbound: loopback sends no key, remote uses openrouter.
    credential: str | None = None
    # Provider-specific JSON fields added to every chat request this
    # tier makes (TD-5008), e.g. a llama.cpp server's
    # ``chat_template_kwargs``. They fill in only fields the request left
    # unset. Sent as-is: never put a secret here.
    extra_body: dict[str, Any] | None = None
    # Merged over ``extra_body`` on the worker's one-word verdict calls
    # only (TD-5012): the classifier, DoD polls and worker-backed
    # judgment calls. An 8-token answer ends inside a reasoning model's
    # thinking, so this is where ``enable_thinking: false`` goes without
    # turning it off for the turn or a delegated worker.
    verdict_extra_body: dict[str, Any] | None = None
    # Provider timeouts in seconds (TD-5106). The defaults are the values
    # every tier used before they were configurable. ``total_timeout``
    # caps one attempt including streaming; ``stream_idle_timeout`` aborts
    # a stream that sends no usable chunk for that long (keep-alive pings
    # do not count). A stall before any output is retried under
    # ``provider_retry``; one partway through a turn's reply is withdrawn
    # and asked once more (TD-5159). null turns either off.
    connect_timeout: float = Field(default=30.0, gt=0)
    read_timeout: float = Field(default=120.0, gt=0)
    total_timeout: float | None = Field(default=None, gt=0)
    stream_idle_timeout: float | None = Field(default=300.0, gt=0)

    def timeouts(self) -> TimeoutConfig:
        """This tier's timeouts for :class:`~tstd.provider.ProviderClient`."""
        return TimeoutConfig(
            connect=self.connect_timeout,
            read=self.read_timeout,
            total=self.total_timeout,
            stream_idle=self.stream_idle_timeout,
        )

    @field_validator("credential")
    @classmethod
    def _blank_credential_is_unbound(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("extra_body", "verdict_extra_body")
    @classmethod
    def _extra_body_leaves_request_keys_alone(
        cls, value: dict[str, Any] | None, info: ValidationInfo
    ) -> dict[str, Any] | None:
        """Refuse keys the request owns (TD-5008, TD-5012).

        ``extra_body`` only fills in fields a request left unset, so one
        of these would be dropped on some requests and sent on others:
        the model, the transcript, the tools, streaming, or the output
        cap that comes from ``max_output_tokens``. ``verdict_extra_body``
        reaches the request the same way, so the same keys are refused.
        """
        if value is None:
            return None
        clashes = sorted(key for key in value if key in EXTRA_BODY_RESERVED_KEYS)
        if clashes:
            raise ValueError(
                f"{info.field_name} cannot set {', '.join(clashes)}: TST Desk sends "
                f"{'it' if len(clashes) == 1 else 'them'} itself. Use slug for the "
                "model and max_output_tokens for the output cap"
            )
        return value

    @field_validator("extra_body", "verdict_extra_body")
    @classmethod
    def _extra_body_is_plain_json(
        cls, value: dict[str, Any] | None, info: ValidationInfo
    ) -> dict[str, Any] | None:
        """Refuse values the request body cannot carry.

        YAML reads an unquoted ``2026-10-09`` as a date. Left in, it
        loads fine and then fails every request the tier makes, far
        from the line that caused it.
        """
        if value is None:
            return None
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{info.field_name} must be plain JSON ({error}). Quote dates, times and "
                "other values YAML would read as something else"
            ) from error
        return value

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

    def verdict_extras(self) -> dict[str, Any] | None:
        """The extras a one-word verdict call sends (TD-5012).

        Every worker verdict path takes its extras from here: the
        classifier, DoD polls and worker-backed judgment calls. With no
        ``verdict_extra_body`` this is ``extra_body`` itself, so the
        request is what TD-5008 sent. Otherwise the two are deep-merged:
        nested maps merge, ``verdict_extra_body`` wins a leaf, and
        neither input is changed.
        """
        if self.verdict_extra_body is None:
            return self.extra_body
        return merge_extra_body(self.extra_body or {}, self.verdict_extra_body)


def merge_extra_body(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge *override* onto *base* into a new dict (TD-5012).

    A key that is a map on both sides merges recursively; anything else
    in *override* replaces what *base* had. The result shares nothing
    mutable with either input.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = merge_extra_body(existing, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


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


@lru_cache(maxsize=1)
def _shipped_search_fallbacks() -> tuple[str, ...]:
    """Fallback search endpoints from the shipped config, not from source.

    The fallback URLs live in the shipped ``config.yaml`` (prime directive
    §2.7: destinations come from configuration, never from a literal — the
    outbound-hosts test enforces it). Reading them here, as the field's
    default, heals existing user copies too: the loader only fills missing
    top-level sections, so a user ``search:`` section from before this key
    existed would otherwise pin an empty list forever. An explicit empty
    list still disables fallback.
    """
    try:
        data = yaml.safe_load(default_config_yaml())
    except (OSError, yaml.YAMLError):
        return ()
    if not isinstance(data, dict):
        return ()
    search = data.get("search")
    if not isinstance(search, dict):
        return ()
    urls = search.get("fallback_base_urls")
    if not isinstance(urls, list):
        return ()
    return tuple(u.strip() for u in urls if isinstance(u, str) and u.strip())


class SearchConfig(BaseModel):
    """Web search and page fetch (TD-609, TD-610).

    ``base_url`` is the first host ``web_search`` tries; each entry of
    ``fallback_base_urls`` is tried in order when the ones before it fail
    or return nothing parseable. ``web_fetch`` takes a URL the user
    approved. Empty ``base_url`` disables the primary only — fallbacks
    still run; all empty disables search. ``fetch_page_chars`` and
    ``fetch_cache_seconds`` size ``web_fetch``'s windows and its
    in-memory page cache (TD-5114).
    """

    base_url: str = ""
    fallback_base_urls: list[str] = Field(
        default_factory=lambda: list(_shipped_search_fallbacks()),
        description="Search endpoints tried in order after base_url fails",
    )
    timeout_seconds: float = Field(default=15.0, gt=0)
    max_results: int = Field(default=8, ge=1, le=20)
    fetch_max_bytes: int = Field(default=200_000, ge=1)
    # TD-5114: a page is read in windows of this many characters, and kept
    # in memory this long so the next window or question skips the download.
    fetch_page_chars: int = Field(default=30_000, ge=1_000)
    fetch_cache_seconds: int = Field(default=900, ge=0, le=86_400)


class ProjectContextConfig(BaseModel):
    """Pinned-file budget on the brain prompt (TD-2805)."""

    token_budget: int = Field(default=2000, ge=1)


class SteeringConfig(BaseModel):
    """Which extra steering files discovery may read (TD-4845).

    ``claude_global_fallback`` gates ``~/.claude/CLAUDE.md`` and, since
    TD-5011, ``~/.claude/skills/``.  Workspace and nested ``CLAUDE.md``
    files and a workspace's own ``.claude/skills/`` are project files and
    stay on. Default false: TST Desk's global file is
    ``~/.tstdesk/AGENTS.md``, and Claude Code's rules and skill catalog
    are not spent in the prompt or followed into an outside import.
    """

    claude_global_fallback: bool = False


DEFAULT_LOG_MAX_EVENTS = 10000

# 15 minutes. A research turn makes many tool calls; 120 seconds recorded
# it as a timeout, and the retry table then tried the same job again.
DEFAULT_MAX_RUN_SECONDS = 900


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


class SchedulerConfig(BaseModel):
    """How long one turn may run before it is stopped (TD-3819).

    Scheduled runs and ``tst run`` both start from this. A job may name
    its own limit, from 1 to 60 minutes. ``tst run --timeout`` replaces
    this base for one command and still adds the provider retry allowance.
    Above an hour is refused: an unattended slot that needs longer is the
    wrong shape, and the receipt is in minutes of this budget.
    """

    max_run_seconds: float = Field(default=DEFAULT_MAX_RUN_SECONDS, gt=0, le=60 * 60)


class SessionConfig(BaseModel):
    """On-disk session event-log window (TD-2901).

    Count of events, not bytes: attach is ``from_seq``, and a byte cap
    would drop a different prefix than the seq cursor. Zero and omitted
    must not mean unbounded — the default is ``DEFAULT_LOG_MAX_EVENTS``.

    ``send_file_max_bytes`` and ``send_file_session_max_bytes`` cap the
    copies ``send_file`` keeps (TD-5143): per file, and per session in all.
    The 2 GiB per-file ceiling matches both receivers' caps.
    """

    log_max_events: int = Field(default=DEFAULT_LOG_MAX_EVENTS, ge=1)
    send_file_max_bytes: int = Field(default=536_870_912, ge=1, le=2_147_483_648)
    send_file_session_max_bytes: int = Field(default=4_294_967_296, ge=1)


class ClearToolResultsConfig(BaseModel):
    """Clear stale tool results before whole turns compact (TD-5115).

    Once the prompt is over the compaction budget, the tool results of
    earlier turns become, in one batch, a one-line placeholder naming the
    tool, its target and the size that went. The newest
    ``keep_recent_tool_results`` results and every result of a tool in
    ``exclude_tools`` stay, and so does every ``ask_user`` answer
    (``context_clear.ALWAYS_KEPT_TOOLS``, TD-5121). A clear that cannot free
    ``clear_at_least_tokens`` does not happen: rewriting an old result
    costs a local server its cached prefix, so it has to buy room
    (see :mod:`tstd.context_clear`).
    """

    enabled: bool = True
    keep_recent_tool_results: int = Field(default=3, ge=0)
    clear_at_least_tokens: int = Field(default=2_000, ge=0)
    exclude_tools: list[str] = Field(default_factory=list)

    @field_validator("exclude_tools", mode="before")
    @classmethod
    def _clean_tool_names(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("exclude_tools must be a list of tool names")
        names: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("exclude_tools must be a list of tool names")
            name = item.strip()
            if name and name not in names:
                names.append(name)
        return names


class ShellReadOnlyConfig(BaseModel):
    """The read-only shell list (TD-5103): commands that run without asking.

    The list is fixed in code (``READ_ONLY_ENTRIES``). Config can drop
    entries or switch the whole list off with ``enabled: false``; an entry
    that is not built in is a load error, so a config file can never grow
    what auto-runs.
    """

    enabled: bool = True
    commands: list[str] = Field(default_factory=lambda: list(READ_ONLY_ENTRIES))

    @field_validator("commands")
    @classmethod
    def _only_built_in(cls, value: list[str]) -> list[str]:
        unknown = [c for c in value if c not in READ_ONLY_ENTRIES]
        if unknown:
            raise ValueError(
                f"not on the built-in read-only list, which config can only shrink: "
                f"{', '.join(repr(c) for c in unknown)}"
            )
        return list(dict.fromkeys(value))

    def entries(self) -> frozenset[str]:
        """The entries in force: none when the list is off."""
        return frozenset(self.commands) if self.enabled else frozenset()


class ShellConfig(BaseModel):
    """The shell tool's user-level settings (TD-5103)."""

    read_only: ShellReadOnlyConfig = Field(default_factory=ShellReadOnlyConfig)


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
    or ``enabled: false`` means no send.
    """

    enabled: bool = False
    host: str = ""
    timeout_seconds: float = Field(default=5.0, gt=0)


class DiscordNotifyConfig(BaseModel):
    """Discord incoming webhook (TD-4707). Off by default.

    ``host`` is the only host ``tstd.notify.discord.send`` may reach. The
    webhook URL itself is a keychain secret (account ``tst-discord-webhook``),
    never this file, never a log, never the audit database. Empty ``host``
    or ``enabled: false`` means no send.
    """

    enabled: bool = False
    host: str = ""
    timeout_seconds: float = Field(default=5.0, gt=0)


class TelegramNotifyConfig(BaseModel):
    """Telegram Bot API sendMessage (TD-4707). Off by default.

    ``host`` is the only host ``tstd.notify.telegram.send`` may reach. The
    bot URL itself is a keychain secret (account ``tst-telegram-bot``),
    including ``chat_id`` as a query parameter. Never this file, never a
    log, never the audit database. Empty ``host`` or ``enabled: false``
    means no send.
    """

    enabled: bool = False
    host: str = ""
    timeout_seconds: float = Field(default=5.0, gt=0)


_config_log = get_logger("tstd.config")


class EmailNotifyConfig(BaseModel):
    """SMTP report delivery (TD-3820). Off by default.

    ``host`` is the only host ``tstd.notify.email.send`` may reach. The
    password is a keychain secret (account ``tst-smtp-password``), never
    this file. A ``password`` key in yaml is dropped and never stored.
    """

    enabled: bool = False
    # smtp keeps TD-3820. microsoft sends through Graph (TD-4853).
    # google sends through Gmail after Settings signs in (TD-4854).
    method: Literal["smtp", "microsoft", "google"] = "smtp"
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "tls"] = "starttls"
    username: str = ""
    from_address: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _drop_password(cls, data: object) -> object:
        if isinstance(data, dict) and "password" in data:
            # The value must not appear in the warning or in a later
            # validation error that echoes the input.
            _config_log.warning(
                "notify.email.password ignored; the SMTP password lives in the keychain"
            )
            return {key: value for key, value in data.items() if key != "password"}
        return data

    @field_validator("host", "username", "from_address")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


def _bare_host(value: str, label: str) -> str:
    """Bare hostname from config. Empty stays empty so the connector stays off."""
    cleaned = value.strip().lower()
    if not cleaned:
        return ""
    if "://" in cleaned or "/" in cleaned or "@" in cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError(f"{label} must be a bare hostname")
    return cleaned


def _github_host(value: str) -> str:
    return _bare_host(value, "connectors.github host")


# common / organizations / consumers, a tenant GUID, or a domain. No slash:
# the value is one path segment on the login host.
_MICROSOFT_TENANT = re.compile(
    r"^(?:common|organizations|consumers|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)$"
)


class GithubConnectorConfig(BaseModel):
    """GitHub App device flow. Off until a client id and both hosts are set.

    Host defaults are empty here. The shipped ``config.yaml`` names the
    public hosts. Tokens are not fields of this model (TD-4852).
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str = ""
    api_host: str = ""
    web_host: str = ""
    timeout_seconds: float = Field(default=20.0, gt=0)

    @field_validator("client_id")
    @classmethod
    def _strip_client_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_host", "web_host")
    @classmethod
    def _host(cls, value: str) -> str:
        return _github_host(value)

    def enabled(self) -> bool:
        """False when the user has not named an App or a host."""
        return bool(self.client_id and self.api_host and self.web_host)


class MicrosoftConnectorConfig(BaseModel):
    """Microsoft identity platform, public client, auth code + PKCE (TD-4853).

    Off until a client id and both hosts are set. ``tenant`` defaults to
    ``common``. Host defaults are empty here. The shipped ``config.yaml``
    names the public hosts. Tokens are not fields of this model.
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str = ""
    tenant: str = "common"
    login_host: str = ""
    graph_host: str = ""
    timeout_seconds: float = Field(default=20.0, gt=0)
    sign_in_timeout_seconds: float = Field(default=300.0, gt=0)

    @field_validator("client_id")
    @classmethod
    def _strip_client_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("tenant")
    @classmethod
    def _tenant(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return "common"
        if not _MICROSOFT_TENANT.fullmatch(cleaned):
            raise ValueError("connectors.microsoft.tenant is not a tenant id or alias")
        return cleaned

    @field_validator("login_host", "graph_host")
    @classmethod
    def _host(cls, value: str) -> str:
        return _bare_host(value, "connectors.microsoft host")

    def enabled(self) -> bool:
        """False when the user has not named an app registration or a host."""
        return bool(self.client_id and self.login_host and self.graph_host and self.tenant)


class GoogleConnectorConfig(BaseModel):
    """Google installed-app sign-in for Gmail send (TD-4854).

    Off until a client id and every host are set. The desktop client
    secret is not a field: Google does not treat it as confidential, and
    it still lives in the keychain. A ``client_secret`` key in yaml is
    dropped. Host defaults are empty here. The shipped ``config.yaml``
    names the public hosts.
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str = ""
    auth_host: str = ""
    token_host: str = ""
    userinfo_host: str = ""
    gmail_host: str = ""
    timeout_seconds: float = Field(default=20.0, gt=0)
    sign_in_timeout_seconds: float = Field(default=300.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _drop_client_secret(cls, data: object) -> object:
        if isinstance(data, dict) and "client_secret" in data:
            _config_log.warning(
                "connectors.google.client_secret ignored; the client secret lives in the keychain"
            )
            return {key: value for key, value in data.items() if key != "client_secret"}
        return data

    @field_validator("client_id")
    @classmethod
    def _strip_client_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("auth_host", "token_host", "userinfo_host", "gmail_host")
    @classmethod
    def _host(cls, value: str) -> str:
        return _bare_host(value, "connectors.google host")

    def enabled(self) -> bool:
        """False when the user has not named a client id or a host."""
        return bool(
            self.client_id
            and self.auth_host
            and self.token_host
            and self.userinfo_host
            and self.gmail_host
        )


class ConnectorsConfig(BaseModel):
    """Outbound account connectors. Each one is off until configured."""

    model_config = ConfigDict(extra="forbid")

    github: GithubConnectorConfig = Field(default_factory=GithubConnectorConfig)
    microsoft: MicrosoftConnectorConfig = Field(default_factory=MicrosoftConnectorConfig)
    google: GoogleConnectorConfig = Field(default_factory=GoogleConnectorConfig)


class NotifyConfig(BaseModel):
    """Outbound notification channels. Slack is the default; no gateway."""

    slack: SlackNotifyConfig = Field(default_factory=SlackNotifyConfig)
    ntfy: NtfyNotifyConfig = Field(default_factory=NtfyNotifyConfig)
    discord: DiscordNotifyConfig = Field(default_factory=DiscordNotifyConfig)
    telegram: TelegramNotifyConfig = Field(default_factory=TelegramNotifyConfig)
    email: EmailNotifyConfig = Field(default_factory=EmailNotifyConfig)


class SpeechConfig(BaseModel):
    """Hold-to-talk transcription (TD-4701). Off by default.

    ``base_url`` is the OpenAI-compatible transcriptions root (include
    ``/v1``). Empty means unconfigured. There is no shipped cloud URL.
    Optional ``credential`` names a keychain id; the secret is never this
    file, never a log, never the audit database.
    """

    enabled: bool = False
    base_url: str = ""
    model: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0)
    credential: str = ""

    @field_validator("base_url")
    @classmethod
    def _strip_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("model")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        return value.strip()

    @field_validator("credential")
    @classmethod
    def _valid_credential(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return ""
        if cleaned in RESERVED_CREDENTIAL_IDS:
            raise ValueError(
                f"speech.credential {cleaned!r} is reserved for another keychain account"
            )
        if not CREDENTIAL_ID_RE.match(cleaned):
            raise ValueError("speech.credential must be a lowercase slug [a-z][a-z0-9-]{0,31}")
        return cleaned

    @model_validator(mode="after")
    def _base_url_is_http_when_set(self) -> SpeechConfig:
        if not self.base_url:
            return self
        try:
            parts = urlsplit(self.base_url)
        except ValueError as e:
            raise ValueError(f"speech.base_url is not a URL: {e}") from e
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("speech.base_url must be an http(s) OpenAI-compatible endpoint")
        return self


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
    # Hosted vLLM forge is --limit-mm-per-prompt image:1 (TD-1730).
    max_prompt_images: int = Field(default=1, ge=0)

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
    """Opt-in Tailscale bind (TD-3601). Empty is off — loopback only.

    ``port`` pins the listener port (TD-5005). 0 means the port the
    listener last used while remote attach was on, else a random one.
    """

    bind: str = ""
    port: int = 0

    @field_validator("bind")
    @classmethod
    def _strip_bind(cls, value: str) -> str:
        return value.strip()

    @field_validator("port")
    @classmethod
    def _port_range(cls, value: int) -> int:
        if value != 0 and not 1024 <= value <= 65535:
            raise ValueError("remote.port must be 0 or between 1024 and 65535")
        return value


class VoiceConfig(BaseModel):
    """Optional speech-to-text endpoint for hold-to-talk (TD-4701).

    Empty ``base_url`` means the composer uses OS dictation only. A filled
    URL is an OpenAI-compatible ``/audio/transcriptions`` root; the host
    comes from this file, never from Python. Dictation itself is off until
    Settings turns it on (``voice.yaml`` in the daemon's data directory).
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    credential: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0)

    @field_validator("base_url", "credential")
    @classmethod
    def _strip_voice(cls, value: str) -> str:
        return value.strip()


class EngineConfig(BaseModel):
    """Which agent loop a new session uses.

    ``native`` is the TST 3-tier OpenAI-compatible loop. ``grok`` spawns
    the installed Grok Build CLI over ACP. Auth for grok stays in the
    CLI (``~/.grok/auth.json``); this file never holds those credentials.
    Empty ``binary`` means PATH, then ``~/.grok/bin/grok``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["native", "grok"] = "native"
    binary: str = ""

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("binary")
    @classmethod
    def _binary(cls, value: str) -> str:
        return value.strip()


class AutonomyConfig(BaseModel):
    """Autonomy and interactive-verify settings.

    ``runtime`` / ``image`` are the rootless container used only for
    autonomous runs (TD-4301). Interactive sessions ignore those two.
    ``check_every`` is the autonomy drift-check cadence (TD-4201).
    ``verify`` is the interactive validator review after writes (TD-4204,
    spec §12.6 first sentence). It is not the autonomy supervisor.
    ``verify_max_prompt_tokens`` caps that review's estimated prompt;
    null is 60% of the validator tier's window (TD-5153).
    """

    model_config = ConfigDict(extra="forbid")

    runtime: str = Field(default="podman", min_length=1)
    image: str = Field(default="docker.io/library/alpine:3.21", min_length=1)
    check_every: int = Field(default=5, ge=1)
    verify: Literal["off", "after_write", "ask"] = "after_write"
    verify_max_prompt_tokens: int | None = Field(default=None, ge=1024)

    @field_validator("runtime", "image")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class McpServerConfig(BaseModel):
    """One user-listed MCP server (TD-4401).

    Listed in user-data-dir config — no dynamic discovery. HTTP
    destinations must be loopback; the client refuses anything else
    before dialing. ``env`` (stdio) and ``headers`` (http) hold a literal
    that is not a secret, or ``keychain:<entry>`` (TD-5110): the token
    itself stays in the keychain and reaches only this server.
    """

    model_config = ConfigDict(extra="forbid")

    transport: Literal["stdio", "http"]
    command: list[str] = Field(default_factory=list)
    url: str = ""
    enabled: bool = True
    #: Seconds a tool call may go without a result or a progress
    #: notification (TD-5105).  None keeps the 30 s default.
    timeout_seconds: float | None = Field(default=None, gt=0)
    #: Seconds for the handshake and ``tools/list``.  None keeps 8 s.
    startup_timeout_seconds: float | None = Field(default=None, gt=0)
    #: Hard cap on one call, however much progress it reports.  None
    #: keeps 600 s.
    max_call_seconds: float | None = Field(default=None, gt=0)
    #: Extra env for a stdio child, on top of the daemon's own (TD-5110).
    env: dict[str, str] = Field(default_factory=dict)
    #: Extra headers on every request to an http server (TD-5110).
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("env", mode="before")
    @classmethod
    def _env_values(cls, value: Any) -> dict[str, str]:
        return coerce_map("env", value)

    @field_validator("headers", mode="before")
    @classmethod
    def _header_values(cls, value: Any) -> dict[str, str]:
        return coerce_map("header", value)

    @field_validator("command", mode="before")
    @classmethod
    def _command_is_argv(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            raise ValueError("command must be a list of argv tokens, not a string")
        if isinstance(value, list):
            return [str(item) for item in value]
        raise ValueError("command must be a list of argv tokens")

    @field_validator("url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        return value.strip()


class McpConfig(BaseModel):
    """User-listed MCP servers. Empty means none — no doctor rows."""

    model_config = ConfigDict(extra="forbid")

    servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def _valid_server_ids(cls, value: dict[str, McpServerConfig]) -> dict[str, McpServerConfig]:
        bad = sorted(sid for sid in value if not CREDENTIAL_ID_RE.match(sid))
        if bad:
            raise ValueError(
                "mcp server id(s) must be a lowercase slug "
                f"[a-z][a-z0-9-]{{0,31}}: {', '.join(bad)}"
            )
        return value


class JudgmentsConfig(BaseModel):
    """Provider-neutral judgment features (TD-708/709/710/711, dev build).

    Everything defaults off: with all flags false the dispatch and
    autonomy paths are byte-identical to the pre-seam product.  The
    decision classifier's own fallback (TD-703) is not gated here — it
    is the seam's default connector and is always on.  ``backend``
    selects the connector: ``worker`` (the configured worker tier) or
    ``typesafe`` (a hosted typed-judgment API, opt-in; the key lives in
    the OS keychain under ``typesafe_credential``, never here).
    """

    verification: bool = False
    semantic_breaker: bool = False
    candidate_selection: bool = False
    confidence_threshold: float = 0.6
    max_state_chars: int = 2000
    backend: Literal["worker", "typesafe"] = "worker"
    # Empty means "not configured": the typesafe connector cannot run
    # without a URL and the wiring falls back to the worker tier.  The
    # real default URL ships in config.yaml — destinations live in config
    # (data), never as source literals (§2.3).
    typesafe_base_url: str = ""
    typesafe_model: str = "jev-latest"
    typesafe_credential: str = "typesafe"


class ModelConfig(BaseModel):
    """Top-level model configuration loaded from config.yaml."""

    presets: dict[str, Preset]
    active_preset: str = DEFAULT_PRESET
    credentials: dict[str, CredentialConfig] = Field(default_factory=dict)
    search: SearchConfig = Field(default_factory=SearchConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    project_context: ProjectContextConfig = Field(default_factory=ProjectContextConfig)
    steering: SteeringConfig = Field(default_factory=SteeringConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    clear_tool_results: ClearToolResultsConfig = Field(default_factory=ClearToolResultsConfig)
    shell: ShellConfig = Field(default_factory=ShellConfig)
    provider_retry: ProviderRetryConfig = Field(default_factory=ProviderRetryConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    computer_use: ComputerUseConfig = Field(default_factory=ComputerUseConfig)
    judgments: JudgmentsConfig = Field(default_factory=JudgmentsConfig)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

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


def config_yaml_path(data_dir: Path | None = None) -> Path:
    """``config.yaml`` for a daemon data directory, or the library default.

    ``None`` is ``user_data_dir() / "config.yaml"`` — callers that have no
    daemon. A daemon passes the directory it was started with, including
    when that directory is the platform default, so an explicit
    ``--data-dir`` is never resolved by calling this with ``None``.
    """
    root = data_dir if data_dir is not None else user_data_dir()
    return root / "config.yaml"


def ensure_user_config(path: Path | None = None) -> Path:
    """Copy the shipped default config to the resolved path if missing.

    Returns that path. ``None`` is the library file
    (:func:`config_yaml_path`). Both streams are closed before return,
    including when the copy raises: an unclosed handle is reported later
    as ``ResourceWarning`` when it is collected (TD-4835).
    """
    config_path = path if path is not None else config_yaml_path()
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        source = resources.files("tstd").joinpath(_DEFAULT_CONFIG_RESOURCE)
        with source.open("rb") as src, config_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
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
        "steering",
        "session",
        "clear_tool_results",
        "scheduler",
        "computer_use",
        "remote",
        "notify",
        "speech",
        "autonomy",
        "engine",
        "voice",
        "mcp",
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
