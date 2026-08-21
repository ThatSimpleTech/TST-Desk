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

from .artifacts import ArtifactError, ArtifactRecord, ArtifactStore, to_entry
from .attachments import AttachmentError, decode_attachments, render_user_content
from .audit import AuditStore
from .audit_queries import UsageBucket, export_csv, export_jsonl, usage_rollup
from .audit_writer import AuditWriter
from .boundary_config import (
    boundary_source,
    load_workspace_boundary,
    scaffold_workspace_config,
)
from .config import (
    ConfigError,
    ModelConfig,
    ModelDiscoveryError,
    cached_config,
    is_loopback_url,
    load_config,
)
from .config_write import save_active_preset, save_tier_slug
from .context.assembler import ContextAssembler
from .context.instructions import (
    InstructionNameError,
    create_rule_file,
    list_workspace_instructions,
)
from .context.memory_loader import list_workspace_memory
from .context.prompt import PromptAssembler
from .context.stack import build_instruction_stack
from .context_pins import PinOutsideError, add_pin, list_pin_cards, project_capacity, remove_pin
from .coworker import load_coworker, save_coworker
from .desktop import DesktopDriver, desktop_driver_from_config
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
from .logging import get_logger, setup_logging, user_data_dir
from .loop import ProviderLike, agent_loop
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
    Cancel,
    CheckCuPermissions,
    ClientMessageT,
    ContextPinEntry,
    ContextPins,
    CreateRule,
    CuPermissions,
    DaemonEvent,
    DeleteApiKey,
    DeleteSession,
    Deny,
    Detach,
    DiagnosticCheck,
    DiagnosticsReport,
    EndSession,
    ExportUsage,
    ForkFrom,
    GetInstructionStack,
    GetSetupState,
    GetUsage,
    HandshakeError,
    InstructionFileEntry,
    InstructionFiles,
    ListArtifacts,
    ListInstructions,
    ListMemory,
    ListPins,
    ListPolicyRules,
    ListSessions,
    LogTrimmed,
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
    SaveMemory,
    SessionList,
    SessionSummary,
    SetApiKey,
    SetBranch,
    SetCoworker,
    SetLoadGlobalMemory,
    SetPreset,
    SetSessionStar,
    SetSkipAllApprovals,
    SetTier,
    SetTierSlug,
    SetupState,
    SetWorkspacePin,
    Shutdown,
    TierState,
    UsageExported,
    UsageReport,
    UsageRollup,
    UserMessage,
    ValidateApiKey,
    build_error,
    parse_client_message,
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
    auth_failure_message,
)
from .router import TIER_NAMES, TierRouter
from .session import (
    TERMINAL_STATES,
    Session,
    SessionEventLog,
    SessionRegistry,
    SessionRunner,
)
from .session_lifecycle import archive_session, delete_session, move_session, rename_session
from .session_persist import LoadedSession, SessionPersist
from .session_stars import load_session_stars, save_session_stars
from .session_store import SessionStore
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


def _tier_state_event(session_id: str, router: TierRouter, config: ModelConfig) -> TierState:
    """Build the ``tier_state`` event for the title bar (TD-1006).

    A tier whose slug is still unresolved is omitted rather than sent as a
    placeholder (TD-1805): the daemon does not yet know its model, and the
    UI must never be handed a truth it wasn't given.  The loop re-emits
    ``tier_state`` once discovery lands on the first turn.
    """
    return TierState(
        session_id=session_id,
        tier=router.active_tier,
        override=router.override,
        model_slugs={
            name: slug for name in TIER_NAMES if (slug := config.tier(name).slug) is not None
        },
        seq=1,  # overwritten by the event log
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
        self._artifacts = ArtifactStore(self._session_persist)
        self._slug_snapshot = _snapshot_slugs(self.config)
        self._provider = provider
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
        self.workspace_pins = load_workspace_pins(self.data_dir)
        self.session_stars = load_session_stars(self.data_dir)
        self.ws_server = WebSocketServer(
            self.data_dir,
            message_handler=self._handle_message,
            on_disconnect=self._on_connection_closed,
        )
        # Shared across sessions so the kill-switch is process-wide.
        # Empty computer_use.command is the mock; a command is stdio MCP.
        self.desktop_driver: DesktopDriver = desktop_driver_from_config(self.config)

    def set_computer_use_killed(self, killed: bool) -> None:
        """Stop or resume desktop actuation. Capture still works (TD-3301)."""
        self.desktop_driver.set_killed(killed)

    async def _brain_client(self) -> ProviderClient:
        """Build a client for the active brain tier.

        A loopback base URL is on-box, so there is nothing to authenticate
        against: the keychain is never consulted and no key prompt can occur
        (TD-1801). Remote endpoints keep the keychain requirement unchanged —
        this removes a requirement, it never invents a credential.
        """
        tier_cfg = self.config.tier("brain")
        if is_loopback_url(tier_cfg.base_url):
            return ProviderClient(base_url=tier_cfg.base_url, api_key=None)
        return await ProviderClient.from_keychain(tier_cfg.base_url)

    async def _ensure_provider(self) -> ProviderLike:
        """Create the shared provider client on first use.

        Uses the brain tier's base URL for the OpenAI-compatible endpoint;
        for a remote endpoint the API key comes from the OS keychain.
        """
        if self._provider is None:
            self._provider = await self._brain_client()
        return self._provider

    async def _setup_state_event(self) -> SetupState:
        """Current onboarding state (TD-1101): key presence + preset choice.

        ``has_api_key`` is the wizard's first-run signal; presence is probed
        from the keychain, so it survives daemon restarts and never touches
        the key value itself.  ``key_required`` says whether the active preset
        needs one at all, so a local-only workspace is never prompted for a
        key it will never send (TD-1801).
        """
        try:
            await get_api_key()
            has_api_key = True
        except KeychainError:
            has_api_key = False
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
            pinned_workspaces=list(self.workspace_pins),
        )

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

    async def _provider_probe(self, api_key: str | None = None) -> ProviderError | None:
        """One-token live call against the active brain (TD-1101).

        With *api_key* the probe authenticates with that key directly
        (TD-1106); otherwise the stored key is read from the keychain.
        Returns None on success, the ProviderError on failure.  Raises
        KeychainError only when the keychain is consulted and fails, and
        ModelDiscoveryError when a local tier names no model and the
        endpoint cannot supply one (TD-1805) — there is nothing to probe
        with until that is settled.
        """
        await resolve_tier_slugs(self.config)
        tier_cfg = self.config.tier("brain")
        if api_key is not None:
            client = ProviderClient(base_url=tier_cfg.base_url, api_key=api_key)
        else:
            client = await self._brain_client()
        response = await client.chat_completion(
            ChatCompletionRequest(
                model=tier_cfg.require_slug(),
                messages=[ChatMessage(role="user", content="ok")],
                max_tokens=1,
                stream=False,
            )
        )
        return response if isinstance(response, ProviderError) else None

    async def _validate_api_key(self, api_key: str | None = None) -> ApiKeyValidated:
        """Probe a key with one cheap live call (TD-1101, TD-1106).

        With *api_key*, the key typed in the wizard is checked directly,
        independent of keychain state; otherwise the stored key is probed.
        A one-token completion against the active preset's brain tier: the
        cheapest request that still proves the key authenticates.  The key
        value never appears in the response.
        """
        try:
            err = await self._provider_probe(api_key)
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

        base_url = self.config.tier("brain").base_url
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
        steering.  Blocking filesystem calls ride worker threads; the live
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
        try:
            await get_api_key()
            key_present = True
        except KeychainError:
            key_present = False
        key_required = self.config.requires_api_key()
        if key_present or not key_required:
            rows = await self._doctor_key_provider_rows()
            if not key_required:
                # The probe still ran, so the provider verdict is real; only
                # the key verdict is meaningless for a keyless endpoint.
                rows = [_KEYLESS_KEY_ROW if r.name == "api_key" else r for r in rows]
            checks.extend(rows)
        else:
            checks.append(
                DiagnosticCheck(
                    name="api_key",
                    status="fail",
                    detail="no API key stored",
                    fix="Open the setup wizard (title-bar gear) and paste an API key.",
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
            result = await asyncio.to_thread(self._session_persist.append_event, session_id, event)
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

    @staticmethod
    def _version() -> str:
        from . import __version__

        return __version__

    async def _handle_message(self, raw: str, _connection: Any) -> str | None:
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
                decoded = decode_attachments(msg.attachments, found.boundary_config.attachments)
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

            await found.add_user_message(render_user_content(msg.content, decoded))
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
            await found.event_log.add(_tier_state_event(found.id, found.router, self.config))
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

        if isinstance(msg, ListMemory):
            return await self._handle_list_memory(msg)

        if isinstance(msg, SaveMemory):
            return await self._handle_save_memory(msg)

        if isinstance(msg, CreateRule):
            return await self._handle_create_rule(msg)

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

        # ── Onboarding (TD-1101 first-run wizard) ────────────────────
        if isinstance(msg, GetSetupState):
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetApiKey):
            try:
                await store_api_key(msg.api_key)
            except KeychainLockedError as e:
                # TD-1105: unlock guidance, not raw `security` stderr.
                return build_error("keychain_locked", str(e))
            except (KeychainError, NotImplementedError) as e:
                return build_error("key_store_failed", f"Could not store the API key: {e}")
            # Never log the key; the ack is a refreshed setup_state.
            log.info("api key stored in keychain")
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, ValidateApiKey):
            return (await self._validate_api_key(msg.api_key)).model_dump_json()

        if isinstance(msg, DeleteApiKey):
            # TD-1102: key removable from settings. Same ack pattern as
            # set_api_key — the fresh setup_state flips has_api_key.
            try:
                await delete_api_key(msg.provider)
            except KeychainLockedError as e:
                return build_error("keychain_locked", str(e))
            except (KeychainError, NotImplementedError) as e:
                return build_error("key_delete_failed", f"Could not remove the API key: {e}")
            log.info("api key removed from keychain")
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
                self.config = load_config().model_copy(
                    update={"active_preset": self.config.active_preset}
                )
                self._slug_snapshot = _snapshot_slugs(self.config)
            except ConfigError as e:
                # The write landed but the result will not load. Say so rather
                # than serving a stale config that disagrees with the file.
                return build_error("bad_request", f"Saved, but the config no longer loads: {e}")
            log.info(
                "tier slug changed",
                extra={"extra_fields": {"preset": msg.preset, "tier": msg.tier}},
            )
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

        return None

    async def _revive_session(
        self, session_id: str, workspace_path: str, loaded: LoadedSession
    ) -> None:
        """Start a loop on a persisted conversation. Does not invent messages."""
        sess = await self.session_registry.restore(session_id, workspace_path, "idle")
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
        router = sess.router if sess.router is not None else TierRouter()
        sess.router = router

        try:
            sess.boundary_config = load_workspace_boundary(sess.workspace_path)
        except ConfigError as e:
            log.warning(
                "workspace boundary config invalid; using defaults",
                extra={"extra_fields": {"workspace_path": sess.workspace_path, "error": str(e)}},
            )

        try:
            sess.policy = load_policy(sess.workspace_path)
        except ConfigError as e:
            log.warning(
                "workspace policy config invalid; using defaults",
                extra={"extra_fields": {"workspace_path": sess.workspace_path, "error": str(e)}},
            )

        async def get_provider() -> ProviderLike:
            return await self._ensure_provider()

        tool_registry = create_registry()
        tool_dispatcher = ToolDispatcher(tool_registry)
        tool_dispatcher.skip_all_fn = lambda: self.skip_all_approvals
        register_builtin_handlers(
            tool_dispatcher,
            allowed_commands=sess.boundary_config.boundary.shell_allowlist(),
            desktop_driver=self.desktop_driver,
        )

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
        await sess.event_log.add(_tier_state_event(sess.id, sess.router, self.config))

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
        await self._session_store.upsert(sess.id, workspace_path, sess.state)
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
        )
        tracker = found.cost_tracker
        return build_instruction_stack(
            found.id,
            assembled.steering,
            seq=1,
            last_cached_tokens=(tracker.last_cached_prompt_tokens if tracker is not None else None),
            # No tracker means no call has been made, which is the same
            # "nothing observed yet" the tracker itself reports (TD-1811).
            cache_observed=(tracker.cache_observed if tracker is not None else False),
            memory=found.last_memory,
        ).model_dump_json()

    async def _handle_list_instructions(self, msg: ListInstructions) -> str:
        """List a workspace's Instructions files (TD-2802). Not a tool."""
        return await self._instruction_files_reply(msg.workspace_path)

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
