"""TST Desk daemon — async process with graceful lifecycle.

The daemon owns sessions, the WebSocket server, and the event loop.
It is spawned by the Tauri host and communicates via a local WebSocket.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import websockets.exceptions
from pydantic import ValidationError

from .artifacts import ArtifactError, ArtifactRecord, ArtifactStore, to_entry
from .attachments import (
    AttachmentError,
    build_provider_user_content,
    decode_attachments,
    render_user_content,
)
from .audit import AuditStore
from .audit_queries import UsageBucket, export_csv, export_jsonl, usage_rollup
from .audit_writer import AuditWriter
from .autonomy.charter import Charter, CharterError
from .autonomy.charter_io import read_charter_document, write_charter_document
from .autonomy.runner import first_prompt, notify_autonomy_stop
from .autonomy.start import run_autonomy_start
from .boundary_config import (
    boundary_source,
    load_workspace_boundary,
    scaffold_workspace_config,
)
from .browser import BrowserDriver, BrowserError, browser_driver_from_config, normalize_hit
from .config import (
    DEFAULT_CREDENTIAL_ID,
    ConfigError,
    McpServerConfig,
    ModelConfig,
    ModelDiscoveryError,
    TierConfig,
    allocate_credential_id,
    apply_credential_host,
    cached_config,
    credential_base_url,
    is_loopback_url,
    is_openrouter_family,
    load_config,
    resolve_base_url,
    resolve_credential_id,
)
from .config_write import (
    delete_credential_entry,
    delete_mcp_server_entry,
    save_active_preset,
    save_credential,
    save_mcp_server,
    save_tier_credential,
    save_tier_slug,
)
from .context.assembler import ContextAssembler
from .context.commands import list_workspace_commands_async
from .context.instructions import (
    InstructionNameError,
    create_rule_file,
    list_workspace_instructions,
)
from .context.memory_loader import list_workspace_memory
from .context.prompt import PromptAssembler
from .context.skills import list_workspace_skills_async
from .context.stack import build_instruction_stack
from .context_pins import PinOutsideError, add_pin, list_pin_cards, project_capacity, remove_pin
from .coworker import load_coworker, save_coworker
from .cu_indicators import (
    CuIndicatorPrefs,
    load_cu_indicators,
    save_cu_indicators,
    set_current_prefs,
)
from .desktop import DesktopDriver, desktop_driver_from_config
from .desktop.grounding_client import GroundingClient
from .desktop.permissions import (
    cu_permissions_from_report,
    driver_cu_platform,
    is_desktop_tool,
    load_shown,
    mark_shown,
    probe_report,
)
from .desktop.protocol import DesktopError
from .discovery import resolve_tier_slugs
from .keychain import (
    KeychainError,
    KeychainLockedError,
    delete_api_key,
    get_api_key,
    store_api_key,
)
from .local_worker import (
    effective_tier,
    last_cu_surface,
    session_is_cu_heavy,
    titlebar_hosts,
    titlebar_slugs,
)
from .logging import get_logger, setup_logging, user_data_dir
from .loop import ProviderLike, agent_loop
from .mcp.loader import McpSupervisor
from .memory_commit import MemoryCommitter
from .memory_pref import load_global_memory, save_global_memory
from .memory_store import (
    MemoryCapError,
    MemorySaveError,
    save_workspace_memory,
    scaffold_workspace_memory,
)
from .memory_trigger import (
    DistillEmit,
    apply_edits,
    apply_proposal,
    completed_turn_count,
    distill_if_due,
)
from .notify.discord import schedule as schedule_discord_notify
from .notify.ntfy import schedule as schedule_ntfy_notify
from .notify.slack import schedule as schedule_slack_notify
from .notify.telegram import schedule as schedule_telegram_notify
from .policy import (
    add_rule,
    load_approved_imports,
    load_policy,
    load_skip_all,
    propose_always_allow,
    remove_rule,
    save_policy,
    save_skip_all,
)
from .protocol import (
    AddPin,
    AlwaysAllow,
    ApiKeyValidated,
    Approve,
    ArchiveSession,
    Artifact,
    ArtifactList,
    ArtifactReady,
    Attach,
    AutonomyStart,
    Cancel,
    CharterDocument,
    CheckCuPermissions,
    ClientMessageT,
    CommandEntry,
    CommandList,
    ContextPinEntry,
    ContextPins,
    CreateRule,
    CredentialSummary,
    CuKillState,
    CuPermissions,
    DaemonEvent,
    DeleteApiKey,
    DeleteCredential,
    DeleteJob,
    DeleteMcpServer,
    DeleteSession,
    Deny,
    DenyVerify,
    DesignHit,
    DesignHitBox,
    DesignHitTest,
    Detach,
    DiagnosticCheck,
    DiagnosticsReport,
    EndSession,
    ExportUsage,
    ForkFrom,
    GetCharter,
    GetInstructionStack,
    GetSetupState,
    GetUsage,
    HandshakeError,
    InstructionFileEntry,
    InstructionFiles,
    JobEntry,
    JobList,
    ListArtifacts,
    ListCommands,
    ListInstructions,
    ListJobs,
    ListMemory,
    ListPins,
    ListPolicyRules,
    ListSessions,
    LogTrimmed,
    McpServerSummary,
    MemoryAccept,
    MemoryEdit,
    MemoryFileEntry,
    MemoryFiles,
    MemoryReject,
    MoveSession,
    NewSession,
    OpenArtifact,
    OpenWorkspace,
    PolicyRules,
    PolicyRuleSummary,
    RemovePin,
    RenameSession,
    Resume,
    RevokePolicyRule,
    RunDiagnostics,
    RunVerify,
    SaveCharter,
    SaveJob,
    SaveMemory,
    SessionList,
    SessionSummary,
    SetApiKey,
    SetBranch,
    SetCoworker,
    SetCredential,
    SetCuIndicators,
    SetCuKill,
    SetLoadGlobalMemory,
    SetMcpServer,
    SetPlan,
    SetPreset,
    SetRemoteAttach,
    SetSessionPreset,
    SetSessionStar,
    SetSkipAllApprovals,
    SetTier,
    SetTierCredential,
    SetTierSlug,
    SetupState,
    SetWorkspacePin,
    Shutdown,
    StartAutonomy,
    TierState,
    Transcribe,
    UsageExported,
    UsageReport,
    UsageRollup,
    UserMessage,
    ValidateApiKey,
    build_error,
    parse_client_message,
    scrub_wire_json,
)
from .protocol import (
    BoundaryUpdate as BoundaryUpdateEvent,
)
from .protocol import (
    SessionState as SessionStateEvent,
)
from .protocol import (
    TierSwitched as TierSwitchedEvent,
)
from .protocol import (
    ToolCall as ToolCallEvent,
)
from .protocol import (
    ToolResult as ToolResultEvent,
)
from .provider import (
    ChatCompletionRequest,
    ChatMessage,
    ProviderClient,
    ProviderError,
    RetryConfig,
    auth_failure_message,
)
from .remote_attach import (
    bind_spec_when_enabled,
    load_remote_attach,
    save_remote_attach,
)
from .router import TIER_NAMES, TierRouter
from .scheduler.models import DeliverTo, Job, JobDraft, JobValidationError
from .scheduler.runner import (
    RecordingDeliver,
    channel_notify,
    run_due_jobs,
    run_turn_on_daemon,
)
from .scheduler.runner import SendFn as NotifySendFn
from .scheduler.store import delete_job, get_job, list_jobs, save_job
from .session import (
    TERMINAL_STATES,
    QueuedUserMessage,
    Session,
    SessionEventLog,
    SessionRegistry,
    SessionRunner,
)
from .session_lifecycle import archive_session, delete_session, move_session, rename_session
from .session_persist import LoadedSession, SessionPersist
from .session_stars import load_session_stars, save_session_stars
from .session_store import SessionStore
from .speech import transcribe as transcribe_audio
from .tailscale_bind import InterfaceEnumerator, resolve_remote_bind
from .tools import ToolDispatcher, create_registry, register_builtin_handlers
from .workspace_pins import load_workspace_pins, save_workspace_pins
from .ws import WebSocketServer

log = get_logger("tstd.daemon")

# The doctor's api_key verdict when the active preset sends no key (TD-1801).
_KEYLESS_KEY_ROW = DiagnosticCheck(
    name="api_key",
    status="skip",
    detail="not needed — the active preset runs on a local endpoint",
)

# How many buckets of each kind the usage view is sent (TD-1706). Enough
# history to see a trend, bounded so a year-old audit database does not
# put thousands of rows on the socket for a panel showing a dozen.
_USAGE_LIMITS: dict[UsageBucket, int] = {"session": 25, "day": 30, "week": 12}


def _snapshot_slugs(config: ModelConfig) -> dict[str, dict[str, str | None]]:
    """Every preset's slugs as configured, captured before discovery runs.

    ``resolve_tier_slugs`` fills unset slugs *in place* (TD-1805), so after
    one probe a loopback tier carries a tag that was never in the file. The
    settings screen must not show that as the configured value: the field
    would look set, and saving it would pin a model the user deliberately
    left for the endpoint to choose.

    Plain strings, taken at the moment a config is adopted. ``model_copy`` is
    shallow — a copied config shares its tier objects with the original, and
    so shares their mutations — so copying the config is not a snapshot and
    this cannot be derived from ``self.config`` later (TD-1703).
    """
    return {
        name: {tier: getattr(preset, tier).slug for tier in TIER_NAMES}
        for name, preset in config.presets.items()
    }


@dataclass
class DaemonState:
    """Mutable state tracked by the daemon."""

    started_at: float = field(default_factory=time.time)
    active_sessions: int = 0

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at


def _parent_alive(pid: int) -> bool:
    """Return True if the process with the given PID is alive.

    Used by the orphan-prevention watchdog. Kept cross-platform even though
    this story exercises macOS only:
      - POSIX: ``os.kill(pid, 0)`` probes liveness without signalling.
      - Windows: a limited OpenProcess probe (best-effort).
    """
    if os.name == "nt":
        return _win_parent_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not ours — still alive.
        return True
    return True


def _win_parent_alive(pid: int) -> bool:
    """Probe a Windows process with a limited-query handle (best-effort).

    OpenProcess alone is not enough: a dead process's object persists while
    any handle to it is open, so the open succeeding does not mean the
    process lives.  GetExitCodeProcess distinguishes — anything other than
    STILL_ACTIVE (259) means the process has exited.
    """
    try:
        import ctypes

        # ``ctypes.windll`` exists only on Windows and is absent from mypy's
        # POSIX stubs; getattr keeps one spelling clean on every platform.
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return True  # unreachable — caller gates on os.name == "nt"
        process_query_limited = 0x1000
        still_active = 259
        handle = windll.kernel32.OpenProcess(process_query_limited, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            if not windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # query failed — assume alive rather than orphan the session
            return exit_code.value == still_active
        finally:
            windll.kernel32.CloseHandle(handle)
    except Exception:
        # Degrade to "alive" so a watchdog bug never spuriously kills us.
        return True


def _check_git() -> DiagnosticCheck:
    """git reachable on PATH (TD-1104). Runs in a worker thread."""
    path = shutil.which("git")
    if path is None:
        fix = (
            "Install Xcode Command Line Tools (`xcode-select --install`)."
            if sys.platform == "darwin"
            else "Install git and make sure it is on PATH."
        )
        return DiagnosticCheck(name="git", status="fail", detail="git not found on PATH", fix=fix)
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5, check=False
        )
        version = proc.stdout.strip() or "version unknown"
    except (OSError, subprocess.TimeoutExpired):
        version = "version probe failed"
    return DiagnosticCheck(name="git", status="ok", detail=f"{version} ({path})")


def _check_writable(workspace: Path) -> DiagnosticCheck:
    """The workspace accepts a file (TD-1104). Runs in a worker thread.

    Rows travel the wire and the UI's copy-to-clipboard report, so the
    detail names the workspace by its basename — never an absolute path.
    """
    try:
        with tempfile.NamedTemporaryFile(dir=workspace, prefix=".tstd-probe-", delete=True):
            pass
    except OSError as e:
        return DiagnosticCheck(
            name="workspace",
            status="fail",
            detail=f"{workspace.name} is not writable: {e.strerror or e}",
            fix="Fix the folder's permissions or open a different workspace.",
        )
    return DiagnosticCheck(name="workspace", status="ok", detail=f"{workspace.name} is writable")


def _relativize_paths(text: str, workspace: Path) -> str:
    """Keep absolute local paths out of diagnostics rows (TD-1104).

    Import issues embed resolved (symlink-normalized) absolute paths; rows
    travel the wire and end up in the copy-to-clipboard report, so
    workspace paths become relative and home paths become ``~/…`` — still
    enough to locate the file. The workspace prefix is replaced first (it
    usually sits under home, and the longer prefix must win).
    """
    root = workspace.resolve()
    home = Path.home().resolve()
    return text.replace(f"{root}{os.sep}", "").replace(f"{home}{os.sep}", f"~{os.sep}")


def _approved_import_allowlist(workspace: str | Path) -> frozenset[Path]:
    """Durable external-import allowlist for read-only inspectors (TD-505).

    Denied imports are session-scoped by design, so inspectors forward only
    the persisted set — a denied path correctly reads as "would prompt on a
    new session".  A malformed config yields an empty allowlist rather than
    failing the inspection.
    """
    try:
        return frozenset(load_approved_imports(workspace))
    except ConfigError:
        return frozenset()


def _session_model_config(session: Session, fallback: ModelConfig) -> ModelConfig:
    """The catalog snapshot this session is actually calling (TD-1721)."""
    return session.config if session.config is not None else fallback


def _active_tier_vision(session: Session, config: ModelConfig) -> bool:
    """Whether the active tier accepts image attachments (TD-4705)."""
    if session.router is None:
        return False
    tier_cfg = effective_tier(
        config,
        session.router.active_tier,
        cu_heavy=session_is_cu_heavy(session),
    )
    return tier_cfg.vision


def _tier_state_event(session: Session, config: ModelConfig) -> TierState:
    """Build the ``tier_state`` event for the title bar (TD-1006).

    A tier whose slug is still unresolved is omitted rather than sent as a
    placeholder (TD-1805): the daemon does not yet know its model, and the
    UI must never be handed a truth it wasn't given.  The loop re-emits
    ``tier_state`` once discovery lands on the first turn.

    After a computer-use tool (TD-3903) the worker slug is the remapped
    local-worker preset's worker, not the active preset's remote worker.
    """
    assert session.router is not None
    cu_heavy = session_is_cu_heavy(session)
    tier_cfg = effective_tier(config, session.router.active_tier, cu_heavy=cu_heavy)
    return TierState(
        session_id=session.id,
        tier=session.router.active_tier,
        override=session.router.override,
        model_slugs=titlebar_slugs(config, cu_heavy=cu_heavy),
        preset=config.active_preset,
        hosts=titlebar_hosts(config, cu_heavy=cu_heavy),
        plan=session.router.plan_mode,
        vision=tier_cfg.vision,
        seq=1,  # overwritten by the event log
    )


def _job_entry(job: Job) -> JobEntry:
    """Wire shape for a persisted job. The rail lists these; it does not run them."""
    return JobEntry(
        id=job.id,
        workspace=job.workspace,
        instruction=job.instruction,
        cadence=job.cadence,
        next_run=job.next_run,
        deliver_to=job.deliver_to,
        paused=job.paused,
    )


class Daemon:
    """Async daemon process with clean startup and shutdown.

    Usage:
        daemon = Daemon()
        await daemon.run()
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        provider: ProviderLike | None = None,
        parent_pid: int | None = None,
        notify_send: NotifySendFn | None = None,
        scheduler_tick: float = 15.0,
        interfaces: InterfaceEnumerator | None = None,
    ) -> None:
        self.data_dir = data_dir or user_data_dir()
        self.state = DaemonState()
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self.session_registry = SessionRegistry()
        self._session_store = SessionStore(self.data_dir)
        self.config = cached_config()
        self._session_persist = SessionPersist(
            self.data_dir,
            log_max_events=self.config.session.log_max_events,
        )
        # Persist is awaited outside SessionEventLog's seq lock, so two
        # overlapping to_thread appends can take the file lock as 2 then 1
        # (Windows CI). One asyncio lock per session keeps jsonl in seq
        # order.
        self._event_persist_locks: dict[str, asyncio.Lock] = {}
        self._artifacts = ArtifactStore(self._session_persist)
        self._slug_snapshot = _snapshot_slugs(self.config)
        self._provider = provider
        self._clients: dict[tuple[str, str], ProviderLike] = {}
        self._pending_memory: dict[str, DistillEmit] = {}
        # Built when the daemon starts serving — the audit database only
        # appears on disk once the daemon actually runs (TD-902).
        self._audit_writer: AuditWriter | None = None
        # When set, a watchdog task shuts the daemon down if this host dies.
        self._parent_pid = parent_pid
        self._parent_poll_interval = 1.0
        # session_id -> set of attached connections
        self._attached_clients: dict[str, set[Any]] = {}
        # (connection, session_id) -> streaming task
        self._streaming_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        # TD-804: machine-wide skip-all.  Loaded from the user data dir so
        # a workspace file cannot carry it into someone else's clone.
        self.skip_all_approvals = load_skip_all(self.data_dir)
        self.load_global_memory = load_global_memory(self.data_dir)
        self.coworker_enabled = load_coworker(self.data_dir)
        self.remote_attach_enabled, self.remote_attach_last_bind = load_remote_attach(self.data_dir)
        self.cu_indicators = load_cu_indicators(self.data_dir)
        set_current_prefs(self.cu_indicators)
        self.workspace_pins = load_workspace_pins(self.data_dir)
        self.session_stars = load_session_stars(self.data_dir)
        bind = ""
        if self.remote_attach_enabled:
            spec = bind_spec_when_enabled(self.remote_attach_last_bind, self.config.remote.bind)
            self.config.remote.bind = spec
            try:
                if resolve_remote_bind(spec, interfaces) is not None:
                    bind = spec
            except ValueError:
                bind = ""
        else:
            self.config.remote.bind = ""
        self.ws_server = WebSocketServer(
            self.data_dir,
            message_handler=self._handle_message,
            on_disconnect=self._on_connection_closed,
            bind=bind,
            interfaces=interfaces,
        )
        # Shared across sessions so the kill-switch is process-wide.
        # Empty computer_use.command is the mock; a command is stdio MCP.
        self.desktop_driver: DesktopDriver = desktop_driver_from_config(
            self.config, self.cu_indicators
        )
        # Browser CU (TD-1710): mock unless computer_use.browser is playwright
        # and Playwright is importable. Profile lives under the data dir.
        self.browser_driver: BrowserDriver = browser_driver_from_config(self.config, self.data_dir)
        # User-listed MCP servers (TD-4401). Handshake is lazy; a dead
        # server is a doctor row, never a failed Daemon.run.
        self._mcp = McpSupervisor(self.config.mcp)
        self._notify_send = notify_send if notify_send is not None else self._channel_notify
        self._scheduler_tick = scheduler_tick
        self._scheduler_deliver = RecordingDeliver(send=self._notify_send)

    def set_computer_use_killed(self, killed: bool) -> None:
        """Stop or resume desktop actuation. Capture still works (TD-3301)."""
        self.desktop_driver.set_killed(killed)

    async def _build_client(self, tier_cfg: TierConfig) -> ProviderClient:
        """Build a client for *tier_cfg*'s endpoint.

        A bound named key always wins, including on loopback (TD-1717).
        Unbound loopback stays keyless (TD-1801). Unbound remote uses
        the historical ``openrouter`` keychain account.
        """
        retry_cfg = RetryConfig(
            max_retries=self.config.provider_retry.max_retries,
            initial_delay=self.config.provider_retry.initial_delay,
            max_delay=self.config.provider_retry.max_delay,
        )
        cred_id = resolve_credential_id(tier_cfg)
        base_url = resolve_base_url(self.config, tier_cfg)
        if cred_id is None:
            return ProviderClient(base_url=base_url, api_key=None, retry_config=retry_cfg)
        return await ProviderClient.from_keychain(
            base_url, provider_name=cred_id, retry_config=retry_cfg
        )

    async def _brain_client(self) -> ProviderClient:
        """Build a client for the active brain tier."""
        return await self._build_client(self.config.tier("brain"))

    async def _client_for(self, tier_cfg: TierConfig) -> ProviderLike:
        """The provider client for *tier_cfg*, cached by URL and key.

        An injected constructor provider (tests) is returned for every
        tier so a mock stays in front of the loop. Production caches one
        client per ``(base_url, credential)`` so two keys on one host do
        not share a client, and a remapped local worker (TD-3903) does
        not reuse the remote brain client.
        """
        if self._provider is not None:
            return self._provider
        cache_key = (
            resolve_base_url(self.config, tier_cfg),
            resolve_credential_id(tier_cfg) or "",
        )
        cached = self._clients.get(cache_key)
        if cached is not None:
            return cached
        client = await self._build_client(tier_cfg)
        self._clients[cache_key] = client
        return client

    async def _ensure_provider(self) -> ProviderLike:
        """Create the shared provider client on first use.

        Distill and doctor stay on the active brain. Session turns pass
        the effective tier into ``get_provider`` so a CU-heavy worker
        can land on a different URL (TD-3903).
        """
        return await self._client_for(self.config.tier("brain"))

    async def _setup_state_event(self) -> SetupState:
        """Current onboarding state (TD-1101): key presence + preset choice.

        ``has_api_key`` is the wizard's first-run signal; presence is probed
        from the keychain, so it survives daemon restarts and never touches
        the key value itself.  ``key_required`` says whether the active preset
        will send a key at all, so a local-only workspace is never prompted
        for a key it will never send (TD-1801).
        """
        credentials = await self._credential_summaries()
        has_api_key = any(item.stored for item in credentials)
        tiers = self.config.tiers()
        return SetupState(
            seq=1,
            has_api_key=has_api_key,
            key_required=self.config.requires_api_key(),
            presets=sorted(self.config.presets),
            active_preset=self.config.active_preset,
            tier_slugs=dict(self._slug_snapshot.get(self.config.active_preset, {})),
            skip_all_approvals=self.skip_all_approvals,
            load_global_memory=self.load_global_memory,
            coworker_enabled=self.coworker_enabled,
            cu_glow=self.cu_indicators.glow,
            cu_agent_cursor=self.cu_indicators.agent_cursor,
            cu_show_on_real_display=self.cu_indicators.show_on_real_display,
            pinned_workspaces=list(self.workspace_pins),
            remote_attach_enabled=self.remote_attach_enabled,
            remote_bind=self.ws_server.extra_host,
            credentials=credentials,
            tier_credentials={name: tiers[name].credential for name in tiers},
            tier_loopback={name: is_loopback_url(tiers[name].base_url) for name in tiers},
            mcp_servers=[
                McpServerSummary(
                    id=sid,
                    transport=spec.transport,
                    command=list(spec.command),
                    url=spec.url,
                    enabled=spec.enabled,
                )
                for sid, spec in sorted(self.config.mcp.servers.items())
            ],
            speech_enabled=self.config.speech.enabled,
            speech_ready=self.config.speech.enabled and bool(self.config.speech.base_url),
        )

    async def _credential_is_stored(self, credential_id: str) -> bool:
        try:
            await get_api_key(credential_id)
            return True
        except (KeychainError, FileNotFoundError):
            return False

    async def _credential_summaries(self) -> list[CredentialSummary]:
        """Catalog rows plus the implicit openrouter slot, never secrets."""
        items: list[tuple[str, str]] = [
            (cid, cfg.name) for cid, cfg in self.config.credentials.items()
        ]
        if DEFAULT_CREDENTIAL_ID not in self.config.credentials:
            items.insert(0, (DEFAULT_CREDENTIAL_ID, "OpenRouter"))
        return [
            CredentialSummary(
                id=cid,
                name=name,
                stored=await self._credential_is_stored(cid),
                base_url=credential_base_url(self.config, cid),
            )
            for cid, name in items
        ]

    def _reload_user_config(self) -> None:
        """Re-read config.yaml, keep the live preset, drop cached clients."""
        self.config = load_config().model_copy(update={"active_preset": self.config.active_preset})
        self._slug_snapshot = _snapshot_slugs(self.config)
        self._clients.clear()

    async def _store_named_key(self, api_key: str, credential: str | None, name: str | None) -> str:
        """Store a secret and ensure its catalog row (TD-1717).

        Wizard path: both optional → ``openrouter`` / "OpenRouter".
        A new name with no id slugifies; a collision gets a numeric suffix.
        """
        existing = set(self.config.credentials)
        display = (name or "").strip()
        if credential:
            cred_id = credential.strip()
        elif display:
            cred_id = allocate_credential_id(display, existing)
        else:
            cred_id = DEFAULT_CREDENTIAL_ID
        if display:
            catalog_name = display
        elif cred_id in self.config.credentials:
            catalog_name = self.config.credentials[cred_id].name
        elif cred_id == DEFAULT_CREDENTIAL_ID:
            catalog_name = "OpenRouter"
        else:
            catalog_name = cred_id
        host = None
        if cred_id not in self.config.credentials and is_openrouter_family(cred_id):
            default = self.config.credentials.get(DEFAULT_CREDENTIAL_ID)
            if default is not None:
                host = default.base_url
        save_credential(cred_id, catalog_name, base_url=host)
        await store_api_key(api_key, cred_id)
        self._reload_user_config()
        return cred_id

    def _upsert_credential_name(self, credential: str | None, name: str) -> str:
        """Create or rename a catalog row without touching the secret."""
        cleaned = name.strip()
        existing = set(self.config.credentials)
        cred_id = credential.strip() if credential else allocate_credential_id(cleaned, existing)
        host = None
        if cred_id not in self.config.credentials and is_openrouter_family(cred_id):
            default = self.config.credentials.get(DEFAULT_CREDENTIAL_ID)
            if default is not None:
                host = default.base_url
        save_credential(cred_id, cleaned, base_url=host)
        self._reload_user_config()
        return cred_id

    async def _delete_named_credential(self, credential_id: str) -> None:
        """Drop catalog row, secret, and tier bindings."""
        cred_id = credential_id.strip()
        bound: list[tuple[str, str]] = []
        for preset_name, preset in self.config.presets.items():
            for tier_name in ("brain", "worker", "validator"):
                tier = getattr(preset, tier_name)
                if tier.credential == cred_id:
                    bound.append((preset_name, tier_name))
        try:
            await delete_api_key(cred_id)
        except KeychainLockedError:
            raise
        except KeychainError:
            # Catalog-only row (never stored a secret) is still removable.
            if cred_id not in self.config.credentials:
                raise
        if cred_id in self.config.credentials:
            delete_credential_entry(cred_id)
        for preset_name, tier_name in bound:
            save_tier_credential(preset_name, tier_name, None)
        self._reload_user_config()

    def _cu_permissions_session(self, event: DaemonEvent) -> str | None:
        """Session to notify about computer-use permissions, or None.

        First desktop tool call (once per platform) and every typed
        integrity refuse (``permission_denied``, ``uipi``,
        ``secure_desktop``).  The panel is connection-scoped — this
        does not persist.
        """
        plat = driver_cu_platform(self.desktop_driver)
        if (
            isinstance(event, ToolCallEvent)
            and is_desktop_tool(event.name)
            and not load_shown(self.data_dir, plat)
        ):
            return event.session_id
        if isinstance(event, ToolResultEvent) and event.error_code in DesktopError.REOPEN_CODES:
            return event.session_id
        return None

    async def _cu_permissions_event(self, *, first_run: bool) -> CuPermissions:
        """Probe the driver without prompting. Stamp first-run when asked."""
        raw = await probe_report(self.desktop_driver)
        if first_run:
            mark_shown(self.data_dir, driver_cu_platform(self.desktop_driver))
        return cu_permissions_from_report(raw, first_run=first_run)

    async def _emit_cu_permissions(self, session_id: str, *, first_run: bool) -> None:
        """Push ``cu_permissions`` to clients attached to *session_id*."""
        event = await self._cu_permissions_event(first_run=first_run)
        payload = event.model_dump_json()
        for conn in list(self._attached_clients.get(session_id, set())):
            with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                await conn.send(payload)

    async def _provider_probe(
        self, api_key: str | None = None, credential: str | None = None
    ) -> ProviderError | None:
        """One-token live call against a tier (TD-1101, TD-1717).

        With *api_key* the probe authenticates with that key directly
        (TD-1106); otherwise the stored key is read from the keychain.
        *credential* picks which stored key and prefers a tier bound to
        it so the probe hits the host that key is meant for.
        Returns None on success, the ProviderError on failure.  Raises
        KeychainError only when the keychain is consulted and fails, and
        ModelDiscoveryError when a local tier names no model and the
        endpoint cannot supply one (TD-1805) — there is nothing to probe
        with until that is settled.
        """
        await resolve_tier_slugs(self.config)
        tier_cfg = self.config.tier("brain")
        if credential:
            for candidate in self.config.tiers().values():
                if resolve_credential_id(candidate) == credential:
                    tier_cfg = candidate
                    break
        tier_cfg = apply_credential_host(self.config, tier_cfg)
        if api_key is not None:
            client = ProviderClient(base_url=tier_cfg.base_url, api_key=api_key)
        elif credential:
            client = await ProviderClient.from_keychain(tier_cfg.base_url, provider_name=credential)
        else:
            client = await self._build_client(tier_cfg)
        response = await client.chat_completion(
            ChatCompletionRequest(
                model=tier_cfg.require_slug(),
                messages=[ChatMessage(role="user", content="ok")],
                max_tokens=1,
                stream=False,
            )
        )
        return response if isinstance(response, ProviderError) else None

    async def _validate_api_key(
        self, api_key: str | None = None, credential: str | None = None
    ) -> ApiKeyValidated:
        """Probe a key with one cheap live call (TD-1101, TD-1106, TD-1717).

        With *api_key*, the key typed in the wizard is checked directly,
        independent of keychain state; otherwise the stored key is probed.
        A one-token completion against a matching tier: the cheapest
        request that still proves the key authenticates.  The key value
        never appears in the response.
        """
        try:
            err = await self._provider_probe(api_key, credential)
        except KeychainError as e:
            return ApiKeyValidated(seq=1, ok=False, detail=str(e))
        except ModelDiscoveryError as e:
            return ApiKeyValidated(seq=1, ok=False, detail=str(e))
        if err is not None:
            detail = auth_failure_message() if err.code == "auth_failed" else err.message
            return ApiKeyValidated(seq=1, ok=False, detail=detail)
        return ApiKeyValidated(
            seq=1, ok=True, detail="Key accepted by the active preset's provider."
        )

    # ── Diagnostics (TD-1104 doctor) ──────────────────────────────────

    def _current_workspace(self) -> Path | None:
        """Workspace of the most recently used session, if any."""
        records = self._session_store.records()
        if not records:
            return None
        return Path(max(records, key=lambda r: r.updated_at).workspace_path)

    async def _doctor_key_provider_rows(self) -> list[DiagnosticCheck]:
        """The ``api_key`` and ``provider`` rows from one live probe.

        Both AC rows share a single one-token call: an auth failure means
        the provider IS reachable (it answered) but the key is bad; a
        transport failure means we never got far enough to judge the key.
        """
        fix_key = "Re-enter a valid key: title-bar gear → Provider API key."
        try:
            err = await self._provider_probe()
        except KeychainError as e:
            return [
                DiagnosticCheck(name="api_key", status="fail", detail=str(e), fix=fix_key),
                DiagnosticCheck(
                    name="provider",
                    status="skip",
                    detail="not checked — no API key to send",
                ),
            ]
        except ModelDiscoveryError as e:
            # The endpoint is the subject here, not the key: we never got a
            # model to ask for, so no request was made and the key verdict
            # would be invented (TD-1805).
            return [
                DiagnosticCheck(
                    name="api_key",
                    status="skip",
                    detail="not checked — no model resolved to probe with",
                ),
                DiagnosticCheck(name="provider", status="fail", detail=str(e), fix=e.fix),
            ]

        base_url = resolve_base_url(self.config, self.config.tier("brain"))
        if err is None:
            return [
                DiagnosticCheck(name="api_key", status="ok", detail="accepted by the provider"),
                DiagnosticCheck(name="provider", status="ok", detail=f"reachable at {base_url}"),
            ]
        if err.code in ("connection_error", "timeout"):
            return [
                DiagnosticCheck(
                    name="api_key",
                    status="skip",
                    detail="cannot validate — the provider is unreachable",
                ),
                DiagnosticCheck(
                    name="provider",
                    status="fail",
                    detail=f"unreachable at {base_url} ({err.code})",
                    fix="Check the network or VPN, and the provider base_url in config.yaml.",
                ),
            ]
        if err.code == "auth_failed":
            return [
                DiagnosticCheck(
                    name="api_key",
                    status="fail",
                    detail="provider rejected the stored key (401)",
                    fix=fix_key,
                ),
                DiagnosticCheck(name="provider", status="ok", detail=f"reachable at {base_url}"),
            ]
        # Any other error still means the provider answered.
        return [
            DiagnosticCheck(name="api_key", status="ok", detail="accepted by the provider"),
            DiagnosticCheck(
                name="provider", status="ok", detail=f"answered at {base_url} ({err.code})"
            ),
        ]

    async def _diagnostics_report(self) -> DiagnosticsReport:
        """Run the doctor checks (TD-1104) and return the report.

        Rows in display order: daemon → key → provider → git → workspace →
        steering → optional ``mcp:<id>`` (only when ``mcp.servers`` is
        non-empty).  Blocking filesystem calls ride worker threads; the live
        provider probe is the only slow row (one token, worst case the
        provider timeout).
        """
        checks: list[DiagnosticCheck] = [
            # The reply itself proves reachability; the row carries versions
            # so a pasted report is self-describing.
            DiagnosticCheck(
                name="daemon",
                status="ok",
                detail=f"responding (v{self._version()}, up {self.state.uptime:.1f}s)",
            )
        ]

        # API key presence first — presence is free, validity needs the probe.
        needed: list[str] = []
        for tier in self.config.tiers().values():
            cid = resolve_credential_id(tier)
            if cid and cid not in needed:
                needed.append(cid)
        missing = [cid for cid in needed if not await self._credential_is_stored(cid)]
        key_required = bool(needed)
        key_present = key_required and not missing
        if key_present or not key_required:
            rows = await self._doctor_key_provider_rows()
            if not key_required:
                # The probe still ran, so the provider verdict is real; only
                # the key verdict is meaningless for a keyless endpoint.
                rows = [_KEYLESS_KEY_ROW if r.name == "api_key" else r for r in rows]
            checks.extend(rows)
        else:
            names = [
                self.config.credentials[cid].name if cid in self.config.credentials else cid
                for cid in missing
            ]
            labeled = ", ".join(names)
            checks.append(
                DiagnosticCheck(
                    name="api_key",
                    status="fail",
                    detail=f"no API key stored for {labeled}",
                    fix="Open Settings → API keys and store the missing key.",
                )
            )
            checks.append(
                DiagnosticCheck(
                    name="provider",
                    status="skip",
                    detail="not checked — no API key to send",
                )
            )

        checks.append(await asyncio.to_thread(_check_git))

        workspace = self._current_workspace()
        if workspace is None:
            checks.append(
                DiagnosticCheck(name="workspace", status="skip", detail="no workspace open yet")
            )
            checks.append(
                DiagnosticCheck(name="steering", status="skip", detail="no workspace open yet")
            )
        else:
            checks.append(await asyncio.to_thread(_check_writable, workspace))
            checks.append(await self._check_steering(workspace))

        if self.config.mcp.servers:
            try:
                await self._mcp.ensure_loaded()
            except Exception:
                log.exception("mcp doctor probe failed")
            for row in self._mcp.doctor_rows():
                checks.append(
                    DiagnosticCheck(
                        name=f"mcp:{row.server_id}",
                        status=row.status,
                        detail=row.detail,
                        fix=row.fix,
                    )
                )

        return DiagnosticsReport(seq=1, checks=checks)

    # ── Usage and cost (TD-1706) ───────────────────────────────────────
    #
    # Reads open their own connection rather than borrowing the audit
    # writer's. The writer's single drain task is what makes one sqlite
    # connection safe to use from the thread pool; a query sharing it
    # would run beside a write on that same connection and lose the
    # guarantee. WAL (set in AuditStore) is what lets a second connection
    # read while the first writes, so a separate handle is the supported
    # way to ask, not a workaround.

    def _audit_reader(self) -> AuditStore:
        return AuditStore(self.data_dir / "audit.db")

    async def _usage_report(self) -> UsageReport:
        """Every usage bucket, split by tier, from the audit store."""

        def _query() -> list[UsageRollup]:
            store = self._audit_reader()
            try:
                return [
                    UsageRollup(
                        bucket=row.bucket,
                        key=row.key,
                        tier=row.tier,
                        prompt_tokens=row.prompt_tokens,
                        cached_prompt_tokens=row.cached_prompt_tokens,
                        completion_tokens=row.completion_tokens,
                        cost=row.cost,
                        classifier_cost=row.classifier_cost,
                    )
                    for bucket, limit in _USAGE_LIMITS.items()
                    for row in usage_rollup(store, bucket, limit)
                ]
            finally:
                store.close()

        return UsageReport(seq=1, rows=await asyncio.to_thread(_query))

    async def _usage_export(self, fmt: Literal["jsonl", "csv"]) -> str:
        """Write the TD-903 export to the daemon's exports directory."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.data_dir / "exports" / f"usage-{stamp}.{fmt}"

        def _write() -> int:
            store = self._audit_reader()
            try:
                writer = export_csv if fmt == "csv" else export_jsonl
                return writer(store, path)
            finally:
                store.close()

        try:
            rows = await asyncio.to_thread(_write)
        except OSError as e:
            # A half-written export must not be reported as a success.
            return build_error("export_failed", f"Could not write the usage export: {e}")
        log.info(
            "usage exported",
            extra={"extra_fields": {"format": fmt, "rows": rows}},
        )
        return UsageExported(seq=1, format=fmt, path=str(path), rows=rows).model_dump_json()

    async def _check_steering(self, workspace: Path) -> DiagnosticCheck:
        """Steering stack parses: resolution runs and imports land."""
        try:
            assembled = await ContextAssembler().assemble(
                workspace, approved_imports=_approved_import_allowlist(workspace)
            )
        except Exception as e:  # resolution is designed not to raise; report if it does
            return DiagnosticCheck(
                name="steering",
                status="fail",
                detail=_relativize_paths(f"steering resolution failed: {e}", workspace),
                fix="Fix the malformed steering file and re-run Doctor.",
            )
        issues = assembled.import_issues
        if issues:
            more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
            return DiagnosticCheck(
                name="steering",
                status="fail",
                detail=_relativize_paths(f"{issues[0]}{more}", workspace),
                fix="Fix or remove the broken @import in the named file.",
            )
        return DiagnosticCheck(
            name="steering",
            status="ok",
            detail=f"{len(assembled.sources)} steering source(s) parsed",
        )

    async def run(self) -> None:
        """Start the daemon and run until shutdown is requested."""
        log.info(
            "starting",
            extra={"extra_fields": {"version": self._version(), "data_dir": str(self.data_dir)}},
        )

        # Ensure data directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Audit writer before revive so restored loops can record calls.
        self._audit_writer = AuditWriter(AuditStore(self.data_dir / "audit.db"))
        self._audit_writer.start()

        # Rehydrate sessions from the registry plus any persisted
        # transcript. A session with a conversation snapshot is revived
        # for real; one without is an interrupted tombstone.
        await self._restore_sessions()

        # Register signal handlers.  asyncio's add_signal_handler is
        # POSIX-only — it raises NotImplementedError on Windows, where
        # shutdown still arrives via the shutdown message, the parent
        # watchdog, or the console control event the host sends.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._on_signal)

        # If the host told us its PID, watch it: if the host dies (including
        # by force-quit) we must not become an orphan.
        if self._parent_pid is not None:
            self._tasks.append(asyncio.create_task(self._parent_watchdog(self._parent_pid)))

        # Start subsystems
        try:
            await self._serve()
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

        log.info(
            "stopped",
            extra={"extra_fields": {"uptime": f"{self.state.uptime:.1f}s"}},
        )

    async def _serve(self) -> None:
        """Main serving loop — start subsystems and wait for shutdown."""
        if self._audit_writer is None:
            self._audit_writer = AuditWriter(AuditStore(self.data_dir / "audit.db"))
            self._audit_writer.start()
        await self.ws_server.start()
        self._tasks.append(asyncio.create_task(self._scheduler_loop()))
        await self._shutdown_event.wait()

    async def _shutdown(self) -> None:
        """Graceful shutdown: distill, cancel tasks, close sockets, flush."""
        log.info("shutting down")
        await self._distill_live_sessions()

        # Stop the WebSocket server (closes clients and removes the port file)
        await self.ws_server.stop()

        # Stop session loops
        sessions = await self.session_registry.list_sessions()
        for session in sessions:
            runner = self.session_registry.get_runner(session.id)
            if runner is not None:
                await runner.cancel()

        # Cancel streaming tasks
        for task in list(self._streaming_tasks.values()):
            if not task.done():
                task.cancel()
        if self._streaming_tasks:
            await asyncio.gather(*self._streaming_tasks.values(), return_exceptions=True)
            self._streaming_tasks.clear()

        # Cancel remaining daemon tasks (e.g. the parent watchdog)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

        # Drain audit writes before closing the store (state flushed)
        if self._audit_writer is not None:
            await self._audit_writer.close()
            self._audit_writer = None

        await self.desktop_driver.aclose()
        await self.browser_driver.aclose()
        await self._mcp.aclose()

        log.info("shutdown complete")

    def _on_signal(self) -> None:
        """Handle OS signals for graceful shutdown."""
        log.info("signal received")
        self._shutdown_event.set()

    async def _restore_sessions(self) -> None:
        """Rehydrate the registry. Revive only when a conversation snapshot exists.

        Events without a conversation are loaded for replay so the window
        can show what was saved — the session stays ``interrupted`` and
        will not accept a new message. That is the honest reading: we
        have a transcript, not a model context.
        """
        for record in self._session_store.records():
            chosen = self._resolved_preset(record.preset)
            if record.preset != chosen:
                await self._session_store.set_preset(record.session_id, chosen)
            loaded = await asyncio.to_thread(self._session_persist.load, record.session_id)
            if loaded is not None and loaded.conversation is not None:
                await self._revive_session(record.session_id, record.workspace_path, loaded)
                continue
            final_state = "interrupted"
            if record.state in {"complete", "failed", "cancelled"} and (
                loaded is None or loaded.conversation is None
            ):
                # Terminal with no snapshot stays terminal. Same as before.
                final_state = record.state
            sess = await self.session_registry.restore(
                record.session_id, record.workspace_path, final_state
            )
            sess.preset = chosen
            if loaded is not None and loaded.events:
                sess.event_log.replace(loaded.events)
            if final_state != record.state:
                await self._session_store.update_state(record.session_id, final_state)
            log.info(
                "restored session",
                extra={
                    "extra_fields": {
                        "session_id": record.session_id,
                        "state": final_state,
                        "revived": False,
                    }
                },
            )

    async def _scheduler_loop(self) -> None:
        """On start (revive) and on a short tick, fire each due job once."""
        while not self._shutdown_event.is_set():
            try:
                await self.run_due_jobs()
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._scheduler_tick,
                )
            except TimeoutError:
                continue

    async def _channel_notify(self, channel: DeliverTo, summary: str) -> None:
        """Default scheduler delivery: Slack/ntfy ``send``, never a skip log."""
        await channel_notify(self.config, channel, summary)

    async def run_due_jobs(self, now: datetime | None = None) -> list[str]:
        """Wake due jobs against this daemon. Tests call this directly."""
        when = now if now is not None else datetime.now(UTC)
        return await run_due_jobs(
            self.data_dir,
            when,
            run_turn=self._scheduled_run_turn,
            deliver=self._scheduler_deliver,
        )

    async def _scheduled_run_turn(self, workspace: Path, message: str) -> str:
        """In-process ``tst run``: one session, one message, no nested daemon."""
        return await run_turn_on_daemon(self, workspace, message)

    async def _parent_watchdog(self, parent_pid: int) -> None:
        """Poll the host's liveness and shut down if it dies (TD-1002).

        The host can be force-killed in a way no destructor catches, so the
        daemon must notice on its own. ``--parent-pid`` is the contract that
        makes this testable without the real Tauri host.
        """
        while not self._shutdown_event.is_set():
            if not _parent_alive(parent_pid):
                log.warning("parent process gone, shutting down to avoid orphan")
                self._shutdown_event.set()
                return
            await asyncio.sleep(self._parent_poll_interval)

    async def _on_session_event(self, event: DaemonEvent, event_log: SessionEventLog) -> None:
        """Write the event to disk and refresh the registry row."""
        session_id = getattr(event, "session_id", None)
        if isinstance(session_id, str) and session_id:
            persist_lock = self._event_persist_locks.setdefault(session_id, asyncio.Lock())
            async with persist_lock:
                result = await asyncio.to_thread(
                    self._session_persist.append_event, session_id, event
                )
            if result.trimmed:
                await event_log.drop_before(result.earliest_seq)
        if isinstance(event, SessionStateEvent):
            await self._session_store.update_state(event.session_id, event.state)
        announce = self._cu_permissions_session(event)
        if announce is not None:
            first_run = not load_shown(self.data_dir, driver_cu_platform(self.desktop_driver))
            self._tasks.append(
                asyncio.create_task(self._emit_cu_permissions(announce, first_run=first_run))
            )
        task = schedule_slack_notify(self.config, event)
        if task is not None:
            self._tasks.append(task)
        task = schedule_ntfy_notify(self.config, event)
        if task is not None:
            self._tasks.append(task)
        task = schedule_discord_notify(self.config, event)
        if task is not None:
            self._tasks.append(task)
        task = schedule_telegram_notify(self.config, event)
        if task is not None:
            self._tasks.append(task)

    @staticmethod
    def _version() -> str:
        from . import __version__

        return __version__

    async def _handle_message(self, raw: str, _connection: Any) -> str | None:
        """Post-handshake entry point — and the reply redaction chokepoint.

        Every direct reply funnels through here and is scrubbed before it
        reaches the socket (TD-4802): connection-scoped replies bypass the
        session event log, where redaction happens at insertion (TD-1405).
        Broadcast and replay carry log events, already scrubbed at
        insertion, so they do not pass through here.
        """
        reply = await self._dispatch_message(raw, _connection)
        if reply is None:
            return None
        return scrub_wire_json(reply)

    async def _dispatch_message(self, raw: str, _connection: Any) -> str | None:
        """Handle a post-handshake message from a client.

        Routes messages to the session layer. Returns a response string
        or None to send nothing.
        """
        try:
            msg: ClientMessageT = parse_client_message(raw)
        except HandshakeError as e:
            return build_error(e.code, e.message)

        if isinstance(msg, OpenWorkspace):
            # Validate before creating anything (TD-1103): a nonexistent or
            # non-directory path must not silently "succeed".
            workspace_path = Path(msg.path)
            # Blocking stat off the event loop (ASYNC240 precedent: TD-1104
            # diagnostics use to_thread for the same reason).
            if not await asyncio.to_thread(workspace_path.is_dir):
                return build_error(
                    "workspace_not_found",
                    f"Workspace path is not a directory: {msg.path}",
                )
            # Plant the commented config template on first open (TD-1103) —
            # never overwrites an existing config.
            await asyncio.to_thread(scaffold_workspace_config, workspace_path)
            return await self._start_session(msg.path)

        if isinstance(msg, NewSession):
            # Anchor on an existing session: its workspace becomes the new
            # session's workspace (TD-1701). The path comes from the
            # registry, not the client — no re-validation, no config
            # scaffold. Memory templates are planted in _start_session.
            anchor = self.session_registry.get(msg.session_id)
            if anchor is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            return await self._start_session(anchor.workspace_path)

        if isinstance(msg, UserMessage):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            # Liveness honesty (TD-1711): a terminal session — or any
            # session whose loop is gone (restored tombstone, cancelled
            # while idle) — can never consume the message. Enqueueing
            # anyway would void it silently (observed 2026-08-14); refuse
            # with an actionable error the UI can render.
            runner = self.session_registry.get_runner(msg.session_id)
            if found.state in TERMINAL_STATES or runner is None or not runner.is_running:
                return build_error(
                    "session_not_running",
                    f"This session is {found.state} and can no longer run turns; "
                    "the message was not delivered. Start a new session and resend it.",
                    session_id=msg.session_id,
                )
            # Attachment caps and the text/binary test are enforced here
            # (TD-1709), not in the composer: the client is not trustworthy,
            # so a UI-only gate is no gate at all. A refusal drops the whole
            # message — the copy says so — rather than delivering a turn the
            # user believes carried files it did not.
            try:
                allow_images = _active_tier_vision(found, _session_model_config(found, self.config))
                decoded = decode_attachments(
                    msg.attachments,
                    found.boundary_config.attachments,
                    allow_images=allow_images,
                )
            except AttachmentError as e:
                log.info(
                    "attachment refused",
                    extra={
                        "extra_fields": {
                            "session_id": msg.session_id,
                            "code": e.code,
                            "attachment_count": len(msg.attachments),
                        }
                    },
                )
                return build_error(e.code, e.message, session_id=msg.session_id)

            display = render_user_content(msg.content, decoded)
            provider = build_provider_user_content(msg.content, decoded)
            await found.add_user_message(QueuedUserMessage(display=display, provider=provider))
            # Title from the user's text, not the rendered body — an
            # attachment-only message must stay untitled (TD-3001).
            await self._session_store.maybe_set_title(msg.session_id, msg.content)
            log.info(
                "user message enqueued",
                extra={
                    "extra_fields": {
                        "session_id": msg.session_id,
                        "content_length": len(msg.content),
                        "attachment_count": len(decoded),
                        "attachment_bytes": sum(a.size for a in decoded),
                    }
                },
            )
            return None

        if isinstance(msg, ForkFrom):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            result = await found.fork_from(msg.user_index, msg.content)
            if isinstance(result, str):
                return build_error(result, result.replace("_", " "), session_id=msg.session_id)
            await found.event_log.add(result)
            return None

        if isinstance(msg, SetBranch):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            result = await found.set_branch(msg.user_index, msg.sibling_index)
            if isinstance(result, str):
                return build_error(result, result.replace("_", " "), session_id=msg.session_id)
            await found.event_log.add(result)
            return None

        if isinstance(msg, Cancel):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            await self.session_registry.cancel(msg.session_id)
            self.state.active_sessions = max(0, self.state.active_sessions - 1)
            # Return the cancellation event
            events = found.event_log.events_from(found.event_log.last_seq)
            if events:
                return events[0].model_dump_json()
            return None

        if isinstance(msg, (RunVerify, DenyVerify)):
            # TD-4204 ask mode: the parked verify lives on the session,
            # not an approval card. Unknown session is the same miss as
            # cancel; a session with nothing pending is a silent no-op.
            from .autonomy.verify import confirm_pending_verify, deny_pending_verify

            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            if isinstance(msg, RunVerify):
                await confirm_pending_verify(found)
            else:
                await deny_pending_verify(found)
            return None

        if isinstance(msg, Resume):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            # Reload the workspace boundary so raised caps take effect;
            # the loop re-checks caps before its next model call and
            # re-pauses if the new caps are still exceeded.
            try:
                found.boundary_config = load_workspace_boundary(found.workspace_path)
            except ConfigError as e:
                log.warning(
                    "boundary config invalid on resume; keeping current caps",
                    extra={"extra_fields": {"session_id": msg.session_id, "error": str(e)}},
                )
            await found.resume()
            cfg = found.boundary_config
            await found.event_log.add(
                BoundaryUpdateEvent(
                    session_id=found.id,
                    writable_paths=list(cfg.boundary.writable_paths),
                    allowed_commands=list(cfg.boundary.allowed_commands),
                    network=cfg.boundary.network,
                    spend_usd=cfg.caps.spend_usd,
                    wall_clock_hours=cfg.caps.wall_clock_hours,
                    max_iterations=cfg.caps.max_iterations,
                    source="resume",
                    attachments=cfg.attachments,
                    seq=1,
                )
            )
            return None

        if isinstance(msg, SetPlan):
            return await self._handle_set_plan(msg)

        if isinstance(msg, SetSessionPreset):
            return await self._handle_set_session_preset(msg)

        if isinstance(msg, SetTier):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            if found.router is None:
                return build_error(
                    "session_not_live",
                    f"Session {msg.session_id!r} has no live agent loop "
                    "(restored after restart); its tier cannot be changed",
                )
            if found.router.plan_mode and msg.tier != "brain":
                return build_error(
                    "plan_mode",
                    "Plan mode is on; only the brain tier is allowed",
                    session_id=found.id,
                )
            try:
                previous = found.router.active_tier
                found.router.set_tier(msg.tier)
            except ValueError as e:
                return build_error("bad_request", str(e))
            # Timeline entry for the manual switch (TD-1005)...
            await found.event_log.add(
                TierSwitchedEvent(
                    session_id=found.id,
                    tier=msg.tier,
                    previous=previous,
                    seq=1,  # overwritten by event log
                )
            )
            # ...then acknowledge with the new state so the title bar snaps
            # over even before the next turn starts (TD-1006).
            await found.event_log.add(
                _tier_state_event(found, _session_model_config(found, self.config))
            )
            log.info(
                "tier override set",
                extra={
                    "extra_fields": {
                        "session_id": found.id,
                        "tier": msg.tier,
                    }
                },
            )
            return None

        if isinstance(msg, (Approve, Deny)):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            # Resolution reaches the parked dispatcher via the
            # session-owned future; the session stays parked regardless
            # of which client (or none) is attached (TD-802).
            approved = isinstance(msg, Approve)
            detail = msg.reason if isinstance(msg, Deny) else None
            if not found.resolve_approval(msg.tool_call_id, approved, detail):
                return build_error(
                    "no_pending_approval",
                    f"No pending approval {msg.tool_call_id!r} in session {msg.session_id!r}",
                )
            return None

        if isinstance(msg, AlwaysAllow):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            pending = found.get_pending_approval(msg.tool_call_id)
            if pending is None:
                return build_error(
                    "no_pending_approval",
                    f"No pending approval {msg.tool_call_id!r} in session {msg.session_id!r}",
                )
            # External-import approvals (TD-505) have no tool; they are
            # class-C reads and can never be always-allowed (TD-803 c4).
            if pending.tool is None:
                return build_error(
                    "class_c_not_always_allowable",
                    "External imports can never be always-allowed",
                )
            # The narrowest rule, never a blanket grant; None when the
            # call is a class-C decision (TD-803 criterion 4).
            rule = propose_always_allow(
                pending.tool,
                pending.arguments,
                pending.decision_class,
                Path(found.workspace_path),
            )
            if rule is None:
                return build_error(
                    "class_c_not_always_allowable",
                    "Class C actions can never be always-allowed",
                )
            found.policy = add_rule(found.policy, rule)
            try:
                save_policy(found.workspace_path, found.policy)
            except ConfigError as e:
                return build_error("policy_save_failed", f"Failed to save policy: {e}")
            # Resolve the parked call as approved so the loop proceeds.
            found.resolve_approval(msg.tool_call_id, True)
            return None

        if isinstance(msg, ListPolicyRules):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            return PolicyRules(
                seq=1,
                rules=[
                    PolicyRuleSummary(tool=r.tool, args=r.args, effect=r.effect)
                    for r in found.policy.rules
                ],
            ).model_dump_json()

        if isinstance(msg, RevokePolicyRule):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            if not remove_rule(found.policy, msg.tool, msg.args):
                return build_error(
                    "policy_rule_not_found",
                    f"No policy rule {msg.tool!r}: {msg.args!r} to revoke",
                )
            try:
                save_policy(found.workspace_path, found.policy)
            except ConfigError as e:
                return build_error("policy_save_failed", f"Failed to save policy: {e}")
            return PolicyRules(
                seq=1,
                rules=[
                    PolicyRuleSummary(tool=r.tool, args=r.args, effect=r.effect)
                    for r in found.policy.rules
                ],
            ).model_dump_json()

        if isinstance(msg, SetSkipAllApprovals):
            self.skip_all_approvals = msg.enabled
            save_skip_all(self.data_dir, msg.enabled)
            if msg.enabled:
                for session in await self.session_registry.list_sessions():
                    session.resolve_skippable_approvals()
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetLoadGlobalMemory):
            self.load_global_memory = msg.enabled
            save_global_memory(self.data_dir, msg.enabled)
            for session in await self.session_registry.list_sessions():
                session.load_global_memory = msg.enabled
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetCoworker):
            self.coworker_enabled = msg.enabled
            save_coworker(self.data_dir, msg.enabled)
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetRemoteAttach):
            return await self._handle_set_remote_attach(msg)

        if isinstance(msg, SetCuIndicators):
            self.cu_indicators = CuIndicatorPrefs(
                glow=msg.glow,
                agent_cursor=msg.agent_cursor,
                show_on_real_display=msg.show_on_real_display,
            )
            save_cu_indicators(self.data_dir, self.cu_indicators)
            set_current_prefs(self.cu_indicators)
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetWorkspacePin):
            pins = [p for p in self.workspace_pins if p != msg.path]
            if msg.pinned:
                pins.append(msg.path)
            self.workspace_pins = pins
            save_workspace_pins(self.data_dir, pins)
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, Attach):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            return await self._handle_attach(msg, found, _connection)

        if isinstance(msg, Detach):
            return await self._handle_detach(msg, _connection)

        if isinstance(msg, ListSessions):
            return await self._handle_list_sessions()

        # ── Session lifecycle (TD-1715) ──────────────────────────────
        # Each verb answers with the refreshed list, so one reply carries
        # both the acknowledgement and the rebinding the client needs when
        # the session it was following just left the default view.
        if isinstance(msg, ArchiveSession):
            refusal = await archive_session(self._session_store, msg.session_id, msg.archived)
            return refusal if refusal is not None else await self._handle_list_sessions()

        if isinstance(msg, SetSessionStar):
            if self._session_store.get(msg.session_id) is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                    session_id=msg.session_id,
                )
            stars = [i for i in self.session_stars if i != msg.session_id]
            if msg.starred:
                stars.append(msg.session_id)
            self.session_stars = stars
            save_session_stars(self.data_dir, stars)
            return await self._handle_list_sessions()

        if isinstance(msg, DeleteSession):
            refusal = await delete_session(
                self.session_registry,
                self._session_store,
                msg.session_id,
                self._release_session,
                persist=self._session_persist,
            )
            if refusal is None:
                self.session_stars = [i for i in self.session_stars if i != msg.session_id]
                save_session_stars(self.data_dir, self.session_stars)
            return refusal if refusal is not None else await self._handle_list_sessions()

        if isinstance(msg, MoveSession):
            refusal = await move_session(
                self.session_registry,
                self._session_store,
                msg.session_id,
                msg.workspace_path,
            )
            return refusal if refusal is not None else await self._handle_list_sessions()

        if isinstance(msg, RenameSession):
            refusal = await rename_session(self._session_store, msg.session_id, msg.title)
            return refusal if refusal is not None else await self._handle_list_sessions()

        if isinstance(msg, GetInstructionStack):
            return await self._handle_get_instruction_stack(msg)

        if isinstance(msg, ListInstructions):
            return await self._handle_list_instructions(msg)

        if isinstance(msg, ListCommands):
            return await self._handle_list_commands(msg)

        if isinstance(msg, ListMemory):
            return await self._handle_list_memory(msg)

        if isinstance(msg, SaveMemory):
            return await self._handle_save_memory(msg)

        if isinstance(msg, CreateRule):
            return await self._handle_create_rule(msg)

        if isinstance(msg, GetCharter):
            return await self._handle_get_charter(msg)

        if isinstance(msg, SaveCharter):
            return await self._handle_save_charter(msg)

        if isinstance(msg, StartAutonomy):
            return await self._handle_start_autonomy(msg)

        if isinstance(msg, ListPins):
            return await self._handle_list_pins(msg)

        if isinstance(msg, AddPin):
            return await self._handle_add_pin(msg)

        if isinstance(msg, RemovePin):
            return await self._handle_remove_pin(msg)

        if isinstance(msg, ListArtifacts):
            return await self._handle_list_artifacts(msg)

        if isinstance(msg, OpenArtifact):
            return await self._handle_open_artifact(msg)

        if isinstance(msg, DesignHitTest):
            return await self._handle_design_hit_test(msg)

        if isinstance(msg, ListJobs):
            return await self._handle_list_jobs()

        if isinstance(msg, SaveJob):
            return await self._handle_save_job(msg)

        if isinstance(msg, DeleteJob):
            return await self._handle_delete_job(msg)

        # ── Onboarding (TD-1101 first-run wizard) ────────────────────
        if isinstance(msg, GetSetupState):
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, Transcribe):
            return (await transcribe_audio(self.config, msg.audio_b64, msg.mime)).model_dump_json()

        if isinstance(msg, SetApiKey):
            try:
                cred_id = await self._store_named_key(msg.api_key, msg.credential, msg.name)
            except KeychainLockedError as e:
                # TD-1105: unlock guidance, not raw `security` stderr.
                return build_error("keychain_locked", str(e))
            except (KeychainError, NotImplementedError) as e:
                return build_error("key_store_failed", f"Could not store the API key: {e}")
            except ConfigError as e:
                return build_error("bad_request", str(e))
            # Never log the key; the ack is a refreshed setup_state.
            log.info(
                "api key stored in keychain",
                extra={"extra_fields": {"credential": cred_id}},
            )
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, ValidateApiKey):
            return (await self._validate_api_key(msg.api_key, msg.credential)).model_dump_json()

        if isinstance(msg, DeleteApiKey):
            # TD-1102: key removable from settings. Same ack pattern as
            # set_api_key — the fresh setup_state flips has_api_key.
            try:
                await delete_api_key(msg.provider)
            except KeychainLockedError as e:
                return build_error("keychain_locked", str(e))
            except (KeychainError, NotImplementedError) as e:
                return build_error("key_delete_failed", f"Could not remove the API key: {e}")
            self._clients.clear()
            log.info(
                "api key removed from keychain",
                extra={"extra_fields": {"credential": msg.provider}},
            )
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetCredential):
            try:
                self._upsert_credential_name(msg.credential, msg.name)
            except ConfigError as e:
                return build_error("bad_request", str(e))
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, DeleteCredential):
            try:
                await self._delete_named_credential(msg.credential)
            except KeychainLockedError as e:
                return build_error("keychain_locked", str(e))
            except (KeychainError, NotImplementedError) as e:
                return build_error("key_delete_failed", f"Could not remove the API key: {e}")
            except ConfigError as e:
                return build_error("bad_request", str(e))
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetTierCredential):
            try:
                save_tier_credential(msg.preset, msg.tier, msg.credential or None)
            except ConfigError as e:
                return build_error("bad_request", str(e))
            try:
                self._reload_user_config()
            except ConfigError as e:
                return build_error("bad_request", f"Saved, but the config no longer loads: {e}")
            log.info(
                "tier credential changed",
                extra={"extra_fields": {"preset": msg.preset, "tier": msg.tier}},
            )
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetPreset):
            if msg.name not in self.config.presets:
                return build_error(
                    "unknown_preset",
                    f"Unknown preset {msg.name!r}; declared: "
                    f"{', '.join(sorted(self.config.presets))}",
                )
            try:
                save_active_preset(msg.name)
            except ConfigError as e:
                return build_error("bad_request", str(e))
            # New sessions route on the new preset immediately; existing
            # sessions keep the tier slugs they opened with.  model_copy
            # keeps the process-wide cached instance untouched.
            self.config = self.config.model_copy(update={"active_preset": msg.name})
            self._slug_snapshot = _snapshot_slugs(self.config)
            log.info(
                "active preset changed",
                extra={"extra_fields": {"preset": msg.name}},
            )
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetTierSlug):
            try:
                save_tier_slug(msg.preset, msg.tier, msg.slug)
            except ConfigError as e:
                return build_error("bad_request", str(e))
            # Reload so the edit takes effect on new sessions without a
            # restart. Existing sessions keep the slug they opened with, the
            # same contract set_preset holds them to.
            try:
                self._reload_user_config()
            except ConfigError as e:
                # The write landed but the result will not load. Say so rather
                # than serving a stale config that disagrees with the file.
                return build_error("bad_request", f"Saved, but the config no longer loads: {e}")
            log.info(
                "tier slug changed",
                extra={"extra_fields": {"preset": msg.preset, "tier": msg.tier}},
            )
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetMcpServer):
            try:
                spec = McpServerConfig(
                    transport=msg.transport,
                    command=msg.command,
                    url=msg.url,
                    enabled=msg.enabled,
                )
                save_mcp_server(msg.id, spec)
            except (ConfigError, ValidationError) as e:
                return build_error("bad_request", str(e))
            try:
                self._reload_user_config()
            except ConfigError as e:
                return build_error("bad_request", f"Saved, but the config no longer loads: {e}")
            await self._mcp.reload(self.config.mcp)
            log.info("mcp server saved", extra={"extra_fields": {"server_id": msg.id}})
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, DeleteMcpServer):
            try:
                delete_mcp_server_entry(msg.id)
            except ConfigError as e:
                return build_error("bad_request", str(e))
            try:
                self._reload_user_config()
            except ConfigError as e:
                return build_error("bad_request", f"Saved, but the config no longer loads: {e}")
            await self._mcp.reload(self.config.mcp)
            log.info("mcp server removed", extra={"extra_fields": {"server_id": msg.id}})
            return (await self._setup_state_event()).model_dump_json()

        # ── Diagnostics (TD-1104 doctor) ─────────────────────────────
        if isinstance(msg, RunDiagnostics):
            return (await self._diagnostics_report()).model_dump_json()

        # ── Usage and cost (TD-1706) ─────────────────────────────────
        if isinstance(msg, GetUsage):
            return (await self._usage_report()).model_dump_json()

        if isinstance(msg, ExportUsage):
            return await self._usage_export(msg.format)

        if isinstance(msg, CheckCuPermissions):
            return (await self._cu_permissions_event(first_run=False)).model_dump_json()

        if isinstance(msg, EndSession):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            if found.turn_in_flight:
                return build_error(
                    "session_busy",
                    "End session waits until the current turn finishes.",
                    session_id=msg.session_id,
                )
            await self._run_distill(found)
            return None

        if isinstance(msg, MemoryAccept | MemoryEdit | MemoryReject):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            parked = self._pending_memory.get(msg.session_id)
            if parked is None or parked.proposal_id != msg.proposal_id:
                return build_error(
                    "no_memory_proposal",
                    "No live memory proposal to accept, edit, or reject.",
                    session_id=msg.session_id,
                )
            if isinstance(msg, MemoryReject):
                del self._pending_memory[msg.session_id]
                return None
            if isinstance(msg, MemoryEdit):
                paths = await apply_edits(found.workspace_path, parked.proposal, msg.files, found)
            else:
                paths = await apply_proposal(found.workspace_path, parked.proposal, found)
            await MemoryCommitter(Path(found.workspace_path)).commit(paths)
            del self._pending_memory[msg.session_id]
            return await self._memory_files_reply(found.workspace_path)

        if isinstance(msg, Shutdown):
            log.info("shutdown requested via websocket")
            self._shutdown_event.set()
            return None

        if isinstance(msg, SetCuKill):
            self.set_computer_use_killed(msg.killed)
            if msg.killed:
                for sess in await self.session_registry.list_sessions():
                    await sess.close_cu_session()
                await self.desktop_driver.set_overlay_session(False)
            # Connection-scoped: the switch is process-wide, so this is
            # not written to any session log (TD-3404).
            return CuKillState(killed=msg.killed).model_dump_json()

        return None

    async def _revive_session(
        self, session_id: str, workspace_path: str, loaded: LoadedSession
    ) -> None:
        """Start a loop on a persisted conversation. Does not invent messages."""
        sess = await self.session_registry.restore(session_id, workspace_path, "idle")
        record = self._session_store.get(session_id)
        self._bind_session_preset(sess, record.preset if record is not None else "")
        if loaded.events:
            sess.event_log.replace(loaded.events)
        if loaded.conversation is not None:
            sess.conversation[:] = loaded.conversation
        await self._attach_session_runtime(sess)
        try:
            source = boundary_source(workspace_path)
        except ConfigError as e:
            source = f"defaults — invalid config ({e})"
        await self._emit_working_context(sess, source)
        log.info(
            "revived session",
            extra={
                "extra_fields": {
                    "session_id": session_id,
                    "events": sess.event_log.last_seq,
                    "messages": len(sess.conversation),
                }
            },
        )

    async def _run_distill(self, session: Session) -> None:
        """Park a proposal and emit ``memory_proposal`` when memory changed."""
        if session.turn_in_flight or completed_turn_count(session) < 1:
            return
        try:
            provider = await self._ensure_provider()
            emitted = await distill_if_due(session, provider, self.config)
        except Exception as exc:
            log.warning(
                "distill skipped; provider failed",
                extra={"extra_fields": {"session_id": session.id, "error": str(exc)}},
            )
            return
        if emitted is None:
            return
        self._pending_memory[session.id] = emitted
        await session.event_log.add(emitted.event)

    async def _distill_live_sessions(self) -> None:
        """Graceful quit: distill every live idle session that had a turn."""
        sessions = await self.session_registry.list_sessions()
        for session in sessions:
            if session.state in TERMINAL_STATES:
                continue
            await self._run_distill(session)

    async def _attach_session_runtime(self, sess: Session) -> None:
        """Boundary, tools, persist hooks, and a running loop for *sess*."""
        sess.load_global_memory = self.load_global_memory
        sess.event_log.subscribe(self._on_session_event)  # type: ignore[arg-type]

        async def _snap() -> None:
            await asyncio.to_thread(
                self._session_persist.save_conversation,
                sess.id,
                list(sess.conversation),
            )

        sess._conversation_hook = _snap
        sess._overlay_session = self.desktop_driver.set_overlay_session
        router = sess.router if sess.router is not None else TierRouter()
        sess.router = router

        try:
            sess.boundary_config = load_workspace_boundary(sess.workspace_path)
        except ConfigError as e:
            log.warning(
                "workspace boundary config invalid; using defaults",
                extra={"extra_fields": {"workspace_path": sess.workspace_path, "error": str(e)}},
            )
        if sess.charter is not None:
            sess.boundary_config.boundary = sess.charter.boundary
            sess.boundary_config.caps = sess.charter.caps

        try:
            sess.policy = load_policy(sess.workspace_path)
        except ConfigError as e:
            log.warning(
                "workspace policy config invalid; using defaults",
                extra={"extra_fields": {"workspace_path": sess.workspace_path, "error": str(e)}},
            )

        async def get_provider(tier_cfg: TierConfig | None = None) -> ProviderLike:
            cfg = _session_model_config(sess, self.config)
            target = tier_cfg if tier_cfg is not None else cfg.tier("brain")
            return await self._client_for(target)

        tool_registry = create_registry()
        tool_dispatcher = ToolDispatcher(tool_registry)
        tool_dispatcher.skip_all_fn = lambda: self.skip_all_approvals
        tool_dispatcher.autonomy_fn = lambda: sess.autonomy
        tool_dispatcher.on_class_c = sess.mark_class_c
        sess.persist_dir = self._session_persist.dir_for(sess.id)
        register_builtin_handlers(
            tool_dispatcher,
            allowed_commands=sess.boundary_config.boundary.shell_allowlist(),
            desktop_driver=self.desktop_driver,
            browser_driver=self.browser_driver,
            grounding_client=GroundingClient.from_config(self.config.computer_use.grounding),
        )
        from .tools.plugins import bind_plugin_handlers

        bind_plugin_handlers(tool_registry, tool_dispatcher)
        try:
            await self._mcp.ensure_loaded()
            self._mcp.attach(tool_registry, tool_dispatcher)
        except Exception:
            log.exception("mcp tool attach failed; builtins still registered")

        sink = self._audit_writer
        runner = SessionRunner(
            sess,
            loop_factory=lambda s: agent_loop(
                s,
                router,
                get_provider,
                self.config,
                tool_registry=tool_registry,
                tool_dispatcher=tool_dispatcher,
                audit_sink=sink,
            ),
        )
        await runner.start()
        if self._audit_writer is not None:
            self._audit_writer.attach_session(sess)
        await self.session_registry.register_runner(sess.id, runner)
        self.state.active_sessions += 1

    async def _emit_working_context(self, sess: Session, source: str) -> None:
        """Log the wall and slugs this loop is actually using.

        Open and revive both call this so the title bar is not left
        showing a previous life's boundary after a restart.
        """
        cfg = sess.boundary_config
        await sess.event_log.add(
            BoundaryUpdateEvent(
                session_id=sess.id,
                writable_paths=list(cfg.boundary.writable_paths),
                allowed_commands=list(cfg.boundary.allowed_commands),
                network=cfg.boundary.network,
                spend_usd=cfg.caps.spend_usd,
                wall_clock_hours=cfg.caps.wall_clock_hours,
                max_iterations=cfg.caps.max_iterations,
                source=source,
                attachments=cfg.attachments,
                seq=1,
            )
        )
        assert sess.router is not None
        await sess.event_log.add(_tier_state_event(sess, _session_model_config(sess, self.config)))

    async def _start_session(self, workspace_path: str) -> str | None:
        """Create, wire, and start a session in ``workspace_path``.

        Shared by ``open_workspace`` and ``new_session`` (TD-1701): both grow
        a session with the same router/boundary/policy/tool wiring; only the
        path's provenance differs (client-supplied and validated vs anchored
        on an existing live session).  Returns the session's first event
        (``session_state``, running) as the wire reply, mirroring
        ``open_workspace``'s response.
        """
        # Memory templates (TD-2101): plant .tst/memory/ on every session
        # start so an already-opened workspace still gets the files. Never
        # overwrites; config scaffold stays OpenWorkspace-only.
        await asyncio.to_thread(scaffold_workspace_memory, workspace_path)
        sess = await self.session_registry.create(workspace_path)
        self._bind_session_preset(sess, self.config.active_preset)
        await self._session_store.upsert(sess.id, workspace_path, sess.state, preset=sess.preset)
        self._session_persist.prepare(sess.id)
        try:
            source = boundary_source(workspace_path)
        except ConfigError as e:
            source = f"defaults — invalid config ({e})"
        await self._attach_session_runtime(sess)
        log.info(
            "session opened",
            extra={
                "extra_fields": {
                    "session_id": sess.id,
                    "workspace_path": workspace_path,
                }
            },
        )
        await self._emit_working_context(sess, source)

        # Return the session_state event (seq=1, "running")
        events = sess.event_log.events_from(1)
        if events:
            return events[0].model_dump_json()
        return None

    def _resolved_preset(self, name: str) -> str:
        """A catalog name, or the live default when *name* is gone."""
        if name in self.config.presets:
            return name
        return self.config.active_preset

    def _bind_session_preset(self, sess: Session, name: str) -> str:
        """Point *sess* at a catalog snapshot. Does not write Settings."""
        chosen = self._resolved_preset(name)
        sess.preset = chosen
        sess.config = self.config.model_copy(update={"active_preset": chosen})
        return chosen

    async def _handle_set_session_preset(self, msg: SetSessionPreset) -> str:
        """Retarget one session at a catalog preset (TD-1721)."""
        found = self.session_registry.get(msg.session_id)
        if found is None:
            return build_error(
                "session_not_found",
                f"Session {msg.session_id!r} not found",
            )
        if found.router is None:
            return build_error(
                "session_not_live",
                f"Session {msg.session_id!r} has no live agent loop "
                "(restored after restart); its preset cannot be changed",
            )
        if found.turn_in_flight:
            return build_error(
                "session_busy",
                "Cannot change preset while a turn is running",
                session_id=found.id,
            )
        if msg.name not in self.config.presets:
            return build_error(
                "unknown_preset",
                f"Unknown preset {msg.name!r}; declared: {', '.join(sorted(self.config.presets))}",
            )
        self._bind_session_preset(found, msg.name)
        await self._session_store.set_preset(found.id, found.preset)
        await found.event_log.add(
            _tier_state_event(found, _session_model_config(found, self.config))
        )
        log.info(
            "session preset set",
            extra={
                "extra_fields": {
                    "session_id": found.id,
                    "preset": found.preset,
                }
            },
        )
        return await self._handle_list_sessions()

    async def _handle_set_plan(self, msg: SetPlan) -> str | None:
        """Turn plan mode on or off and ack with ``tier_state`` (TD-4603)."""
        found = self.session_registry.get(msg.session_id)
        if found is None:
            return build_error(
                "session_not_found",
                f"Session {msg.session_id!r} not found",
            )
        if found.router is None:
            return build_error(
                "session_not_live",
                f"Session {msg.session_id!r} has no live agent loop "
                "(restored after restart); plan mode cannot be changed",
            )
        found.router.set_plan(msg.on)
        await found.event_log.add(
            _tier_state_event(found, _session_model_config(found, self.config))
        )
        log.info(
            "plan mode set",
            extra={
                "extra_fields": {
                    "session_id": found.id,
                    "on": msg.on,
                }
            },
        )
        return None

    async def _handle_set_remote_attach(self, msg: SetRemoteAttach) -> str:
        """Persist the Settings toggle and rebind the extra listener only."""
        if msg.enabled:
            spec = bind_spec_when_enabled(self.remote_attach_last_bind, self.config.remote.bind)
            self.remote_attach_last_bind = spec
            self.config.remote.bind = spec
            try:
                await self.ws_server.apply_bind(spec)
            except (ValueError, OSError):
                # Flag stays on; the address appears once a Tailscale iface exists.
                log.warning(
                    "remote attach enabled but extra bind failed",
                    extra={"extra_fields": {"bind": spec}},
                )
        else:
            if self.config.remote.bind.strip():
                self.remote_attach_last_bind = self.config.remote.bind.strip()
            elif self.ws_server.extra_host:
                self.remote_attach_last_bind = self.ws_server.extra_host
            self.config.remote.bind = ""
            await self.ws_server.apply_bind("")
        self.remote_attach_enabled = msg.enabled
        save_remote_attach(self.data_dir, msg.enabled, self.remote_attach_last_bind)
        return (await self._setup_state_event()).model_dump_json()

    async def _handle_attach(self, msg: Attach, session: Session, connection: Any) -> str | None:
        """Handle an attach: replay events from from_seq, then stream live."""
        session_id = session.id
        conn_key = (id(connection), session_id)

        # A second attach on the same connection supersedes the first — the
        # resume path re-attaches unconditionally (TD-1716), so this is a
        # routine event, not an anomaly.  Cancel the old streamer before the
        # replay so two of them never interleave frames on one socket.
        superseded = self._streaming_tasks.pop(conn_key, None)
        if superseded is not None and not superseded.done():
            superseded.cancel()

        # Replay from from_seq. A rotated prefix is a typed notice, then
        # the same gap/dup rules over whatever is still in the window.
        earliest = session.event_log.earliest_seq
        if msg.from_seq < earliest:
            await connection.send(
                LogTrimmed(
                    session_id=session_id,
                    requested_from_seq=msg.from_seq,
                    earliest_seq=earliest,
                ).model_dump_json()
            )
        replay_from = max(msg.from_seq, earliest)
        for event in session.event_log.events_from(replay_from):
            await connection.send(event.model_dump_json())

        # Register as attached
        if session_id not in self._attached_clients:
            self._attached_clients[session_id] = set()
        self._attached_clients[session_id].add(connection)

        async def _stream() -> None:
            seen_seq = session.event_log.last_seq
            try:
                while True:
                    new_seq = await session.event_log.wait_for_new_event(seen_seq)
                    for event in session.event_log.events_from(seen_seq + 1):
                        await connection.send(event.model_dump_json())
                    seen_seq = new_seq
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                # Clean up on disconnect.  Naming ourselves as the owner keeps
                # a superseded streamer's teardown from tearing down the
                # re-attach that replaced it.
                self._cleanup_attach(connection, session_id, conn_key, owner=asyncio.current_task())

        task = asyncio.create_task(_stream())
        self._streaming_tasks[conn_key] = task
        return None

    async def _handle_list_sessions(self) -> str:
        """Build the durable session list as a ``session_list`` event."""
        starred = set(self.session_stars)
        summaries: list[SessionSummary] = []
        for record in self._session_store.records():
            sess = self.session_registry.get(record.session_id)
            summaries.append(
                SessionSummary(
                    session_id=record.session_id,
                    workspace_path=record.workspace_path,
                    state=cast(Any, record.state),
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    event_count=sess.event_log.last_seq if sess else 0,
                    archived=record.archived,
                    starred=record.session_id in starred,
                    title=record.title,
                    preset=record.preset,
                )
            )
        summaries.sort(key=lambda s: (s.starred, s.updated_at), reverse=True)
        return SessionList(seq=1, sessions=summaries).model_dump_json()

    def _release_session(self, session_id: str) -> None:
        """Drop every client subscription for a session being deleted (TD-1715).

        Each attached connection has a streaming task parked on the session's
        event log; with the session gone they would wait forever on a log
        nothing can append to.
        """
        for connection in list(self._attached_clients.get(session_id, set())):
            self._cleanup_attach(connection, session_id, (id(connection), session_id))
        self._attached_clients.pop(session_id, None)
        self.state.active_sessions = max(0, self.state.active_sessions - 1)

    async def _handle_get_instruction_stack(self, msg: GetInstructionStack) -> str:
        """Assemble and answer with the session's current instruction stack.

        Direct response, not logged — turn-time snapshots already land in
        the event log via the steering-reload push (TD-509).  The session's
        touched paths (TD-503) are forwarded so path-scoped rules report
        their true active/inactive verdict instead of assembling active
        unconditionally.
        """
        found = self.session_registry.get(msg.session_id)
        if found is None:
            return build_error(
                "session_not_found",
                f"Session {msg.session_id!r} not found",
            )
        tier = found.router.active_tier if found.router is not None else "brain"
        assembled = await PromptAssembler(found.workspace_path).assemble(
            tier,
            matched_paths=set(found.touched_paths),
            approved_imports=_approved_import_allowlist(found.workspace_path),
            loaded_skills=list(found.loaded_skills),
        )
        tracker = found.cost_tracker
        skills = await list_workspace_skills_async(found.workspace_path)
        return build_instruction_stack(
            found.id,
            assembled.steering,
            seq=1,
            last_cached_tokens=(tracker.last_cached_prompt_tokens if tracker is not None else None),
            # No tracker means no call has been made, which is the same
            # "nothing observed yet" the tracker itself reports (TD-1811).
            cache_observed=(tracker.cache_observed if tracker is not None else False),
            memory=found.last_memory,
            skills=skills,
            loaded_skill_names=found.loaded_skills,
        ).model_dump_json()

    async def _handle_list_instructions(self, msg: ListInstructions) -> str:
        """List a workspace's Instructions files (TD-2802). Not a tool."""
        return await self._instruction_files_reply(msg.workspace_path)

    async def _handle_list_commands(self, msg: ListCommands) -> str:
        """List a workspace's slash commands (TD-4501). Not a tool."""
        root = Path(msg.workspace_path)
        if not await asyncio.to_thread(root.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {root}",
            )
        listed = await list_workspace_commands_async(root)
        return CommandList(
            workspace_path=str(root),
            commands=[
                CommandEntry(
                    name=c.name,
                    description=c.description,
                    source=c.source,
                    body=c.body,
                    too_large=c.too_large,
                )
                for c in listed
            ],
        ).model_dump_json()

    async def _context_pins_reply(self, workspace: str | Path) -> str:
        root = Path(workspace)
        if not await asyncio.to_thread(root.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {root}",
            )
        cards = await asyncio.to_thread(list_pin_cards, root)
        cap = self.config.project_context.token_budget
        inst, mem, pins, dropped = await asyncio.to_thread(project_capacity, root, cap)
        return ContextPins(
            workspace_path=str(root),
            pins=[
                ContextPinEntry(path=c.path, name=c.name, kind=c.kind, lines=c.lines) for c in cards
            ],
            instruction_tokens=inst,
            memory_tokens=mem,
            pin_tokens=pins,
            capacity_cap=cap,
            dropped=dropped,
        ).model_dump_json()

    async def _handle_list_pins(self, msg: ListPins) -> str:
        return await self._context_pins_reply(msg.workspace_path)

    async def _handle_add_pin(self, msg: AddPin) -> str:
        root = Path(msg.workspace_path)
        if not await asyncio.to_thread(root.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {msg.workspace_path}",
            )
        try:
            await asyncio.to_thread(add_pin, root, msg.path)
        except PinOutsideError as e:
            return build_error("outside_workspace", str(e))
        return await self._context_pins_reply(root)

    async def _handle_remove_pin(self, msg: RemovePin) -> str:
        root = Path(msg.workspace_path)
        if not await asyncio.to_thread(root.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {msg.workspace_path}",
            )
        await asyncio.to_thread(remove_pin, root, msg.path)
        return await self._context_pins_reply(root)

    async def record_artifact(
        self,
        session_id: str,
        title: str,
        mime: str,
        *,
        path: str | None = None,
        content: bytes | None = None,
    ) -> ArtifactRecord:
        """Persist an artifact and emit ``artifact_ready`` (TD-3201).

        Tests and later loop/tool callers use this.  There is no client
        record message and no model tool this story.
        """
        found = self.session_registry.get(session_id)
        if found is None:
            raise ArtifactError(
                "session_not_found",
                f"Session {session_id!r} not found",
            )
        record = await asyncio.to_thread(
            self._artifacts.record,
            session_id,
            title,
            mime,
            Path(found.workspace_path),
            path=path,
            content=content,
        )
        await found.event_log.add(
            ArtifactReady(
                session_id=session_id,
                artifact_id=record.id,
                title=record.title,
                mime=record.mime,
                path=record.path,
                seq=1,
            )
        )
        return record

    async def _handle_list_artifacts(self, msg: ListArtifacts) -> str:
        found = self.session_registry.get(msg.session_id)
        if found is None:
            return build_error(
                "session_not_found",
                f"Session {msg.session_id!r} not found",
                session_id=msg.session_id,
            )
        records = await asyncio.to_thread(
            self._artifacts.list_records,
            msg.session_id,
            Path(found.workspace_path),
        )
        return ArtifactList(
            session_id=msg.session_id,
            artifacts=[to_entry(r) for r in records],
        ).model_dump_json()

    async def _handle_open_artifact(self, msg: OpenArtifact) -> str:
        found = self.session_registry.get(msg.session_id)
        if found is None:
            return build_error(
                "session_not_found",
                f"Session {msg.session_id!r} not found",
                session_id=msg.session_id,
            )
        try:
            record = await asyncio.to_thread(
                self._artifacts.get,
                msg.session_id,
                msg.artifact_id,
                Path(found.workspace_path),
            )
        except ArtifactError as exc:
            return build_error(exc.code, exc.message, session_id=msg.session_id)
        entry = to_entry(record)
        return Artifact(
            session_id=msg.session_id,
            artifact_id=entry.artifact_id,
            title=entry.title,
            mime=entry.mime,
            path=entry.path,
        ).model_dump_json()

    async def _handle_list_jobs(self) -> str:
        return await self._job_list_event()

    async def _handle_save_job(self, msg: SaveJob) -> str:
        try:
            await asyncio.to_thread(self._save_job_record, msg)
        except JobValidationError as exc:
            return build_error("job_invalid", str(exc))
        return await self._job_list_event()

    async def _handle_delete_job(self, msg: DeleteJob) -> str:
        removed = await asyncio.to_thread(delete_job, self.data_dir, msg.job_id)
        if not removed:
            return build_error("job_not_found", f"Job {msg.job_id!r} not found")
        return await self._job_list_event()

    async def _job_list_event(self) -> str:
        jobs = await asyncio.to_thread(list_jobs, self.data_dir)
        return JobList(jobs=[_job_entry(job) for job in jobs]).model_dump_json()

    def _save_job_record(self, msg: SaveJob) -> Job:
        """Persist a draft or an update. Does not run the job."""
        existing = get_job(self.data_dir, msg.id) if msg.id else None
        if existing is not None:
            try:
                updated = Job(
                    id=existing.id,
                    workspace=msg.workspace or existing.workspace,
                    instruction=msg.instruction or existing.instruction,
                    cadence=existing.cadence if msg.cadence is None else msg.cadence,
                    next_run=existing.next_run if msg.next_run is None else msg.next_run,
                    deliver_to=msg.deliver_to or existing.deliver_to,
                    paused=msg.paused,
                )
            except (ValidationError, JobValidationError) as exc:
                raise JobValidationError(str(exc)) from exc
            return save_job(self.data_dir, updated)
        return save_job(
            self.data_dir,
            JobDraft(
                id=msg.id,
                workspace=msg.workspace,
                instruction=msg.instruction,
                cadence=msg.cadence,
                next_run=msg.next_run,
                deliver_to=msg.deliver_to,
                paused=msg.paused,
            ),
        )

    async def _handle_design_hit_test(self, msg: DesignHitTest) -> str:
        """Observe the last CU surface at a CSS-pixel point (TD-3403 / TD-3406)."""
        found = self.session_registry.get(msg.session_id)
        if found is None:
            return build_error(
                "session_not_found",
                f"Session {msg.session_id!r} not found",
                session_id=msg.session_id,
            )
        try:
            if last_cu_surface(found) == "desktop":
                raw = await self.desktop_driver.hit_test(msg.x, msg.y)
            else:
                raw = await self.browser_driver.hit_test(msg.x, msg.y)
        except (BrowserError, DesktopError):
            raw = {}
        node = normalize_hit(raw, msg.x, msg.y)
        box_raw = node.get("box")
        box = DesignHitBox.model_validate(box_raw) if isinstance(box_raw, dict) else None
        xpath = node.get("xpath")
        role = node.get("role")
        attributes = node.get("attributes")
        styles = node.get("styles")
        return DesignHit(
            session_id=msg.session_id,
            x=msg.x,
            y=msg.y,
            xpath=xpath if isinstance(xpath, str) else None,
            role=role if isinstance(role, str) else None,
            attributes=attributes if isinstance(attributes, dict) else {},
            box=box,
            styles=styles if isinstance(styles, dict) else {},
        ).model_dump_json()

    async def _handle_list_memory(self, msg: ListMemory) -> str:
        """List a workspace's Memory files (TD-2601). Not a tool."""
        return await self._memory_files_reply(msg.workspace_path)

    async def _handle_save_memory(self, msg: SaveMemory) -> str:
        """Write a Memory-pane edit through the store and commit it (TD-2602)."""
        root = Path(msg.workspace_path)
        if not await asyncio.to_thread(root.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {msg.workspace_path}",
            )
        try:
            cfg = await asyncio.to_thread(load_workspace_boundary, root)
            path = await asyncio.to_thread(
                save_workspace_memory,
                root,
                msg.path,
                msg.content,
                cfg.memory.max_lines,
            )
        except MemorySaveError as e:
            return build_error("not_a_memory_file", str(e))
        except MemoryCapError as e:
            return build_error("memory_cap", str(e))
        await MemoryCommitter(root).commit([path])
        return await self._memory_files_reply(root)

    async def _memory_files_reply(self, workspace: str | Path) -> str:
        root = Path(workspace)
        if not await asyncio.to_thread(root.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {root}",
            )
        listed = await asyncio.to_thread(list_workspace_memory, root)
        return MemoryFiles(
            workspace_path=str(root),
            files=[
                MemoryFileEntry(path=str(f.path), name=f.name, content=f.content) for f in listed
            ],
        ).model_dump_json()

    async def _handle_get_charter(self, msg: GetCharter) -> str:
        """Load the workspace charter for the pane (TD-4002). Not a tool."""
        root = Path(msg.workspace_path)
        if not await asyncio.to_thread(root.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {msg.workspace_path}",
            )
        try:
            present, charter, notes = await asyncio.to_thread(read_charter_document, root)
        except CharterError as e:
            return build_error("invalid_charter", str(e))
        return CharterDocument(
            workspace_path=str(root),
            present=present,
            charter=charter,
            notes=notes,
        ).model_dump_json()

    async def _handle_save_charter(self, msg: SaveCharter) -> str:
        """Write CHARTER.md as the human (TD-4002). Does not commit."""
        root = Path(msg.workspace_path)
        if not await asyncio.to_thread(root.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {msg.workspace_path}",
            )
        try:
            await asyncio.to_thread(write_charter_document, root, msg.charter, msg.notes)
        except CharterError as e:
            return build_error("invalid_charter", str(e))
        return await self._handle_get_charter(
            GetCharter(workspace_path=str(root)),
        )

    async def _handle_start_autonomy(self, msg: StartAutonomy) -> str:
        """Sign the charter, refuse unless the sandbox is live, then launch."""
        root = Path(msg.workspace_path)
        if not await asyncio.to_thread(root.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {msg.workspace_path}",
            )
        try:
            result = await run_autonomy_start(
                root,
                config=cached_config(),
                charter=msg.charter,
                notes=msg.notes,
            )
        except CharterError as e:
            return build_error("invalid_charter", str(e))
        session_id: str | None = None
        if result.ready and result.charter is not None:
            session_id = await self._launch_autonomy_run(root, result.charter)
        return AutonomyStart(
            workspace_path=str(root),
            ready=result.ready,
            signed=result.signed,
            error=result.error,
            session_id=session_id,
        ).model_dump_json()

    async def _launch_autonomy_run(self, workspace: Path, charter: Charter) -> str:
        """Open a daemon-owned session that iterates without a viewer."""
        await asyncio.to_thread(scaffold_workspace_memory, str(workspace))
        sess = await self.session_registry.create(str(workspace))
        sess.autonomy = True
        sess.charter = charter

        async def _notify(message: str) -> None:
            await notify_autonomy_stop(self.config, message)

        sess.autonomy_notify = _notify
        self._bind_session_preset(sess, self.config.active_preset)
        await self._session_store.upsert(sess.id, str(workspace), sess.state, preset=sess.preset)
        self._session_persist.prepare(sess.id)
        await self._attach_session_runtime(sess)
        await self._emit_working_context(sess, "charter")
        await sess.add_user_message(first_prompt(charter))
        return sess.id

    async def _handle_create_rule(self, msg: CreateRule) -> str:
        """Create a ``.tst/rules/`` file on the human path (TD-2802)."""
        workspace = Path(msg.workspace_path)
        if not await asyncio.to_thread(workspace.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {msg.workspace_path}",
            )
        try:
            created = await asyncio.to_thread(create_rule_file, workspace, msg.name)
        except InstructionNameError as e:
            return build_error("invalid_rule_name", str(e))
        return await self._instruction_files_reply(workspace, created=created)

    async def _instruction_files_reply(
        self, workspace: str | Path, created: Path | None = None
    ) -> str:
        root = Path(workspace)
        if not await asyncio.to_thread(root.is_dir):
            return build_error(
                "workspace_not_found",
                f"Workspace path is not a directory: {root}",
            )
        listed = await asyncio.to_thread(list_workspace_instructions, root)
        return InstructionFiles(
            workspace_path=str(root),
            files=[
                InstructionFileEntry(path=str(f.path), name=f.name, kind=f.kind) for f in listed
            ],
            created=str(created) if created is not None else None,
        ).model_dump_json()

    async def _handle_detach(self, msg: Detach, connection: Any) -> str | None:
        """Handle a detach: stop streaming without affecting the session."""
        session_id = msg.session_id
        conn_key = (id(connection), session_id)

        self._cleanup_attach(connection, session_id, conn_key)
        return None

    def _cleanup_attach(
        self,
        connection: Any,
        session_id: str,
        conn_key: tuple[int, str],
        owner: asyncio.Task[Any] | None = None,
    ) -> None:
        """Remove connection from attached clients and cancel its stream.

        ``owner`` is the streaming task cleaning up after itself.  When the
        slot already belongs to a newer stream — a re-attach on the same
        connection — the cancelled one has nothing left to clean up and must
        not detach the connection the newer stream is serving.
        """
        if owner is not None and self._streaming_tasks.get(conn_key) is not owner:
            return

        # Remove from attached clients
        clients = self._attached_clients.get(session_id)
        if clients:
            clients.discard(connection)
            if not clients:
                del self._attached_clients[session_id]

        # Cancel the streaming task
        task = self._streaming_tasks.pop(conn_key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _on_connection_closed(self, connection: Any) -> None:
        """Clean up all subscriptions for a disconnected client."""
        # Clean up any attach subscriptions for this connection
        for session_id in list(self._attached_clients.keys()):
            conn_key = (id(connection), session_id)
            self._cleanup_attach(connection, session_id, conn_key)

    def health(self) -> dict[str, Any]:
        """Return a health/diagnostics snapshot."""
        return {
            "version": self._version(),
            "uptime": self.state.uptime,
            "active_sessions": self.state.active_sessions,
            "data_dir": str(self.data_dir),
            "shutdown_requested": self._shutdown_event.is_set(),
            "ws_port": self.ws_server.port,
            "ws_clients": self.ws_server.client_count,
        }


def main() -> None:
    """CLI entry point: run the daemon until interrupted."""
    parser = argparse.ArgumentParser(description="TST Desk daemon")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Log level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument("--data-dir", default=None, help="Override the user data directory")
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help="Host process PID to watch: if it exits, the daemon shuts down "
        "instead of becoming an orphan (set by the Tauri host).",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    setup_logging(level=args.log_level)

    async def _run() -> None:
        daemon = Daemon(data_dir=data_dir, parent_pid=args.parent_pid)
        await daemon.run()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
