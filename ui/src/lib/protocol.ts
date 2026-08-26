// TST Desk protocol types — hand-mirrored from core/tstd/protocol.py
// Field naming: snake_case matches the wire format (see DECISIONS.md §TD-204-1)

// ── Base ──────────────────────────────────────────────────────────────

export interface ClientMessage {
  type: string;
}

export interface DaemonEvent {
  type: string;
  seq: number;
}

// ── Client → Daemon ───────────────────────────────────────────────────

export interface Hello extends ClientMessage {
  type: "hello";
  token: string;
  version: number;
}

export interface OpenWorkspace extends ClientMessage {
  type: "open_workspace";
  path: string;
}

/** One text file riding along with a user_message (TD-1709).
 *
 *  `content_b64` carries the file's bytes, not a decode the client made first:
 *  the daemon answers "is this text?" itself, because a UI-only gate is no
 *  gate. Nothing else is declared — a client-stated size or mime type is a
 *  fact the daemon has to re-derive anyway. */
export interface Attachment {
  name: string;
  content_b64: string;
}

/** Attachment caps, from `.tst/config.yaml` via `boundary_update` (TD-1709).
 *  The composer refuses against these rather than a hardcoded guess. */
export interface AttachmentLimits {
  max_file_bytes: number;
  max_total_bytes: number;
  max_count: number;
}

export interface UserMessage extends ClientMessage {
  type: "user_message";
  session_id: string;
  content: string;
  // TD-1709: additive — a client that never sends one behaves as it did.
  // The daemon vets these against the workspace caps and refuses the whole
  // message if any one fails.
  attachments?: Attachment[];
}

/** Replace a past user turn and fork from there (TD-1708). */
export interface ForkFrom extends ClientMessage {
  type: "fork_from";
  session_id: string;
  user_index: number;
  content: string;
}

/** Switch to another sibling at a forked user turn (TD-1708). */
export interface SetBranch extends ClientMessage {
  type: "set_branch";
  session_id: string;
  user_index: number;
  sibling_index: number;
}

export interface Approve extends ClientMessage {
  type: "approve";
  session_id: string;
  tool_call_id: string;
}

export interface Deny extends ClientMessage {
  type: "deny";
  session_id: string;
  tool_call_id: string;
  reason?: string | null;
}

export interface AlwaysAllow extends ClientMessage {
  type: "always_allow";
  session_id: string;
  tool_call_id: string;
}

export interface ListPolicyRules extends ClientMessage {
  type: "list_policy_rules";
  session_id: string;
}

export interface RevokePolicyRule extends ClientMessage {
  type: "revoke_policy_rule";
  session_id: string;
  tool: string;
  args: string;
}

/** Turn skip-all approvals on or off (TD-804). Machine-wide, no session. */
export interface SetSkipAllApprovals extends ClientMessage {
  type: "set_skip_all_approvals";
  enabled: boolean;
}

/** Turn global memory on or off (TD-2603). Machine-wide, no session. */
export interface SetLoadGlobalMemory extends ClientMessage {
  type: "set_load_global_memory";
  enabled: boolean;
}

/** Turn coworker mode on or off (TD-2905). Machine-wide, no session. */
export interface SetCoworker extends ClientMessage {
  type: "set_coworker";
  enabled: boolean;
}

/** Computer-use glow / cursor / real-display overlay (TD-3402). */
export interface SetCuIndicators extends ClientMessage {
  type: "set_cu_indicators";
  glow: boolean;
  agent_cursor: boolean;
  show_on_real_display: boolean;
}

/** Turn Tailscale remote attach on or off (TD-3603). Machine-wide, no session. */
export interface SetRemoteAttach extends ClientMessage {
  type: "set_remote_attach";
  enabled: boolean;
}

/** Pin or unpin a workspace on the Projects list (TD-2806). */
export interface SetWorkspacePin extends ClientMessage {
  type: "set_workspace_pin";
  path: string;
  pinned: boolean;
}

export interface Resume extends ClientMessage {
  type: "resume";
  session_id: string;
}

export interface Cancel extends ClientMessage {
  type: "cancel";
  session_id: string;
}

export interface RunVerify extends ClientMessage {
  type: "run_verify";
  session_id: string;
}

export interface DenyVerify extends ClientMessage {
  type: "deny_verify";
  session_id: string;
}

export interface Attach extends ClientMessage {
  type: "attach";
  session_id: string;
  from_seq: number;
}

export interface Detach extends ClientMessage {
  type: "detach";
  session_id: string;
}

export interface SetTier extends ClientMessage {
  type: "set_tier";
  session_id: string;
  tier: "brain" | "worker" | "validator";
}

/** Turn plan mode (brain lock) on or off (TD-4603). */
export interface SetPlan extends ClientMessage {
  type: "set_plan";
  session_id: string;
  on: boolean;
}

export interface GetInstructionStack extends ClientMessage {
  type: "get_instruction_stack";
  session_id: string;
}

/** List a workspace's Instructions files (TD-2802). Human path. */
export interface ListInstructions extends ClientMessage {
  type: "list_instructions";
  workspace_path: string;
}

/** List a workspace's slash commands (TD-4501). Human path. */
export interface ListCommands extends ClientMessage {
  type: "list_commands";
  workspace_path: string;
}

/** List a workspace's Memory files (TD-2601). Human path. */
export interface ListMemory extends ClientMessage {
  type: "list_memory";
  workspace_path: string;
}

/** Save an edit from the Memory pane (TD-2602). Human path, never a tool. */
export interface SaveMemory extends ClientMessage {
  type: "save_memory";
  workspace_path: string;
  path: string;
  content: string;
}

/** Create a `.tst/rules/` file (TD-2802). Human path, never a tool. */
export interface CreateRule extends ClientMessage {
  type: "create_rule";
  workspace_path: string;
  name: string;
}

/** Load a workspace's signed charter (TD-4002). Human path. */
export interface GetCharter extends ClientMessage {
  type: "get_charter";
  workspace_path: string;
}

/** Spec §12.4 charter fields. Same names as `tstd.autonomy.charter.Charter`. */
export type CharterNetwork = "deny" | string[];

export interface CharterBoundary {
  writable_paths: string[];
  allowed_commands: string[];
  network: CharterNetwork;
}

export interface CharterCaps {
  spend_usd: number;
  wall_clock_hours: number;
  max_iterations: number;
}

export interface CharterFields {
  objective: string;
  definition_of_done: string[];
  source_of_truth: string[];
  boundary: CharterBoundary;
  caps: CharterCaps;
  stop_conditions: string[];
}

/** Write the charter as the human (TD-4002). Never a tool. Does not commit. */
export interface SaveCharter extends ClientMessage {
  type: "save_charter";
  workspace_path: string;
  charter: Record<string, unknown>;
  notes?: string;
}

/** Sign the charter and start an autonomous run (TD-4003). Human path. */
export interface StartAutonomy extends ClientMessage {
  type: "start_autonomy";
  workspace_path: string;
  charter?: Record<string, unknown> | null;
  notes?: string;
}

export interface ListPins extends ClientMessage {
  type: "list_pins";
  workspace_path: string;
}

export interface AddPin extends ClientMessage {
  type: "add_pin";
  workspace_path: string;
  path: string;
}

export interface RemovePin extends ClientMessage {
  type: "remove_pin";
  workspace_path: string;
  path: string;
}

/** Accept a distill proposal as proposed (TD-2401). */
export interface MemoryAccept extends ClientMessage {
  type: "memory_accept";
  session_id: string;
  proposal_id: string;
}

export interface MemoryFileEdit {
  path: string;
  content: string;
}

/** Accept a distill proposal with edited bytes (TD-2401). */
export interface MemoryEdit extends ClientMessage {
  type: "memory_edit";
  session_id: string;
  proposal_id: string;
  files: MemoryFileEdit[];
}

/** Reject a distill proposal. Writes nothing. */
export interface MemoryReject extends ClientMessage {
  type: "memory_reject";
  session_id: string;
  proposal_id: string;
}

/** Run distill for this session (TD-2302). Not a kill. */
export interface EndSession extends ClientMessage {
  type: "end_session";
  session_id: string;
}

export interface Shutdown extends ClientMessage {
  type: "shutdown";
}

export interface ListSessions extends ClientMessage {
  type: "list_sessions";
}

/** Create a fresh session in an existing session's workspace (TD-1701).
 *  `session_id` is the anchor — the daemon replies with the new session's
 *  first event (session_state), exactly like open_workspace. */
export interface NewSession extends ClientMessage {
  type: "new_session";
  session_id: string;
}

/** File a session away, or restore it (TD-1715). Filing, never killing: an
 *  archived session keeps its loop, its log, and any turn in flight. The
 *  daemon answers with a refreshed session_list. */
export interface ArchiveSession extends ClientMessage {
  type: "archive_session";
  session_id: string;
  archived: boolean;
}

/** Star or unstar a session on this machine (TD-3003). Metadata only;
 *  allowed mid-turn. The daemon answers with a refreshed session_list. */
export interface SetSessionStar extends ClientMessage {
  type: "set_session_star";
  session_id: string;
  starred: boolean;
}

/** Destroy a session and its event log (TD-1715) — irreversible, so the rail
 *  confirms first. Refused with `session_busy` while a turn is in flight. */
export interface DeleteSession extends ClientMessage {
  type: "delete_session";
  session_id: string;
}

/** Move to project (TD-1715): reassign the session's workspace. The session
 *  and its event log survive; the agent's cwd and boundary root change on the
 *  next turn. Refused with `session_busy` while a turn is in flight. */
export interface MoveSession extends ClientMessage {
  type: "move_session";
  session_id: string;
  workspace_path: string;
}

/** Rename a session, or restore its auto-title (TD-3002). Empty or
 *  whitespace-only title means restore. Metadata only; allowed mid-turn.
 *  The daemon answers with a refreshed session_list. */
export interface RenameSession extends ClientMessage {
  type: "rename_session";
  session_id: string;
  title: string;
}

// ── Onboarding (TD-1101 first-run wizard) ────────────────────────────

export interface GetSetupState extends ClientMessage {
  type: "get_setup_state";
}

export interface SetApiKey extends ClientMessage {
  type: "set_api_key";
  api_key: string;
  // TD-1717: catalog id (default openrouter) and optional display name.
  credential?: string | null;
  name?: string | null;
}

// TD-1102: key removable from settings. Acked with a fresh setup_state
// (has_api_key flips false), same pattern as set_api_key.
// `provider` is the credential id (kept for the existing wire).
export interface DeleteApiKey extends ClientMessage {
  type: "delete_api_key";
  provider?: string;
}

export interface ValidateApiKey extends ClientMessage {
  type: "validate_api_key";
  // TD-1106: when present, the typed key is checked directly instead of
  // the stored one — validation never depends on keychain state.
  api_key?: string | null;
  // TD-1717: which named key to probe when more than one is stored.
  credential?: string | null;
}

// TD-1717: create or rename a catalog row without touching the secret.
export interface SetCredential extends ClientMessage {
  type: "set_credential";
  name: string;
  credential?: string | null;
}

export interface DeleteCredential extends ClientMessage {
  type: "delete_credential";
  credential: string;
}

export interface SetTierCredential extends ClientMessage {
  type: "set_tier_credential";
  preset: string;
  tier: string;
  credential: string;
}

/** Create or replace one listed MCP server (TD-4403). Command is argv
 *  tokens only — there is no env field. Acked with setup_state. */
export interface SetMcpServer extends ClientMessage {
  type: "set_mcp_server";
  id: string;
  transport: "stdio" | "http";
  command?: string[];
  url?: string;
  enabled?: boolean;
}

/** Remove one listed MCP server (TD-4403). Unknown id is a typed error. */
export interface DeleteMcpServer extends ClientMessage {
  type: "delete_mcp_server";
  id: string;
}

// TD-1703: name the model one tier of one preset uses. Deliberately narrow
// rather than a general config write — a message carrying only a tier and a
// slug cannot smuggle a secret into config.yaml. Acked with a fresh
// setup_state, same pattern as set_preset.
export interface SetTierSlug extends ClientMessage {
  type: "set_tier_slug";
  preset: string;
  tier: string;
  slug: string;
}

export interface SetPreset extends ClientMessage {
  type: "set_preset";
  name: string;
}

// ── Diagnostics (TD-1104 doctor) ─────────────────────────────────────

export interface RunDiagnostics extends ClientMessage {
  type: "run_diagnostics";
}

// TD-1706: ask for the audit store's usage rollups. Connection-scoped —
// the audit database spans every session, so it carries no session id.
export interface GetUsage extends ClientMessage {
  type: "get_usage";
}

// TD-1706: write the model-call records to a file. Format only — the
// destination is the daemon's own exports directory, deliberately not a
// client-supplied path, so this cannot become a "write anywhere" verb.
export interface ExportUsage extends ClientMessage {
  type: "export_usage";
  format: "jsonl" | "csv";
}

/** List artifacts persisted with a session (TD-3201). */
export interface ListArtifacts extends ClientMessage {
  type: "list_artifacts";
  session_id: string;
}

/** Open one artifact by id (TD-3201). Unknown id is a typed error. */
export interface OpenArtifact extends ClientMessage {
  type: "open_artifact";
  session_id: string;
  artifact_id: string;
}

/** Ask the session browser what is at a CSS-pixel point (TD-3403). */
export interface DesignHitTest extends ClientMessage {
  type: "design_hit_test";
  session_id: string;
  x: number;
  y: number;
}

/** Re-probe computer-use OS permissions / integrity (TD-3302, TD-3303). Connection-scoped. */
export interface CheckCuPermissions extends ClientMessage {
  type: "check_cu_permissions";
}

/** Engage or clear the process-wide computer-use kill-switch (TD-3404). */
export interface SetCuKill extends ClientMessage {
  type: "set_cu_kill";
  killed: boolean;
}

/** List persisted scheduled jobs (TD-3805). Connection-scoped. */
export interface ListJobs extends ClientMessage {
  type: "list_jobs";
}

/** Create or replace a scheduled job from draft fields (TD-3805). Pause is this verb. */
export interface SaveJob extends ClientMessage {
  type: "save_job";
  id?: string | null;
  workspace?: string | null;
  instruction?: string | null;
  cadence?: string | null;
  next_run?: string | null;
  deliver_to?: "window" | "slack" | "ntfy" | null;
  paused?: boolean;
}

/** Remove a scheduled job by id (TD-3805). */
export interface DeleteJob extends ClientMessage {
  type: "delete_job";
  job_id: string;
}

export type ClientMessageUnion =
  | Hello
  | OpenWorkspace
  | UserMessage
  | ForkFrom
  | SetBranch
  | Approve
  | Deny
  | AlwaysAllow
  | ListPolicyRules
  | RevokePolicyRule
  | SetSkipAllApprovals
  | SetLoadGlobalMemory
  | SetCoworker
  | SetCuIndicators
  | SetWorkspacePin
  | Resume
  | Cancel
  | RunVerify
  | DenyVerify
  | Attach
  | Detach
  | SetTier
  | SetPlan
  | GetInstructionStack
  | ListInstructions
  | ListCommands
  | ListMemory
  | SaveMemory
  | CreateRule
  | GetCharter
  | SaveCharter
  | StartAutonomy
  | ListPins
  | AddPin
  | RemovePin
  | MemoryAccept
  | MemoryEdit
  | MemoryReject
  | EndSession
  | Shutdown
  | ListSessions
  | NewSession
  | ArchiveSession
  | SetSessionStar
  | DeleteSession
  | MoveSession
  | RenameSession
  | GetSetupState
  | SetApiKey
  | DeleteApiKey
  | ValidateApiKey
  | SetPreset
  | SetTierSlug
  | SetCredential
  | DeleteCredential
  | SetTierCredential
  | SetMcpServer
  | DeleteMcpServer
  | RunDiagnostics
  | GetUsage
  | ExportUsage
  | ListArtifacts
  | OpenArtifact
  | DesignHitTest
  | CheckCuPermissions
  | SetCuKill
  | SetRemoteAttach
  | ListJobs
  | SaveJob
  | DeleteJob;

// ── Daemon → Client ───────────────────────────────────────────────────

/** Out-of-band handshake reply to `hello`. Not a sequenced daemon event. */
export interface HelloAck {
  type: "hello_ack";
  version: number;
}

/** Application-level liveness frame (TD-1716): no session, no seq.
 *
 *  Out-of-band like `hello_ack`, and for the same reason — it is a fact about
 *  the connection, not an event in any session's log — so it is absent from
 *  `DaemonEventUnion` and never reaches a store. The transport consumes it:
 *  the proof it carries is that this page's JavaScript ran at all. */
export interface Ping {
  type: "ping";
}

export interface Ready extends DaemonEvent {
  type: "ready";
  version: string;
  protocol_version: number;
}

export interface SessionState extends DaemonEvent {
  type: "session_state";
  session_id: string;
  state:
    | "idle"
    | "running"
    | "awaiting_approval"
    | "paused"
    | "complete"
    | "failed"
    | "cancelled"
    | "interrupted";
  reason?: string | null;
}

/** The conversation forked or a sibling was selected (TD-1708). */
export interface ConversationReset extends DaemonEvent {
  type: "conversation_reset";
  session_id: string;
  user_index: number;
  sibling_index: number;
  sibling_count: number;
  content: string;
}

export interface UserTurn extends DaemonEvent {
  type: "user_turn";
  session_id: string;
  turn_id: string;
  content: string;
}

export interface AssistantDelta extends DaemonEvent {
  type: "assistant_delta";
  session_id: string;
  delta: string;
}

/** A chunk of a reasoning model's thinking (TD-1901). Same shape as
 *  AssistantDelta, deliberately a different type: the transcript folds one
 *  and shows the other, and only content is the answer. */
export interface AssistantReasoning extends DaemonEvent {
  type: "assistant_reasoning";
  session_id: string;
  delta: string;
}

export interface ToolCall extends DaemonEvent {
  type: "tool_call";
  session_id: string;
  tool_call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  decision_class?: "A" | "B" | "C";
}

export interface ToolResult extends DaemonEvent {
  type: "tool_result";
  session_id: string;
  tool_call_id: string;
  status: "success" | "error";
  output: string;
  truncated: boolean;
  error_code?: string | null;
  diff?: string | null;
}

export interface ShellOutput extends DaemonEvent {
  type: "shell_output";
  session_id: string;
  tool_call_id: string;
  stream: "stdout" | "stderr";
  chunk: string;
}

export interface PolicyRuleSummary {
  tool: string;
  args: string;
  effect: "auto" | "ask" | "never";
}

export interface ApprovalRequest extends DaemonEvent {
  type: "approval_request";
  session_id: string;
  tool_call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  decision_class: "A" | "B" | "C";
  summary: string;
  reason: string;
  proposed_always_allow?: PolicyRuleSummary | null;
}

export interface DecisionLogged extends DaemonEvent {
  type: "decision_logged";
  session_id: string;
  decision_class: "A" | "B" | "C";
  what: string;
  why: string;
  commit?: string | null;
}

export interface CheckpointNotice extends DaemonEvent {
  type: "checkpoint_notice";
  session_id: string;
  code: string;
  message: string;
}

export interface VerifyResult extends DaemonEvent {
  type: "verify_result";
  session_id: string;
  verdict: "pass" | "fail" | "error";
  summary: string;
  cost: number;
  pending?: boolean;
}

export interface CostUpdate extends DaemonEvent {
  type: "cost_update";
  session_id: string;
  turn_cost: number;
  session_cost: number;
  total_cost: number;
  classifier_cost: number;
  /** Session spend per tier (tiers with spend only) — TD-1006. */
  cost_by_tier: Record<string, number>;
}

/** Active tier + configured slugs (TD-1006, TD-1720). */
export interface TierState extends DaemonEvent {
  type: "tier_state";
  session_id: string;
  tier: "brain" | "worker" | "validator";
  override: "brain" | "worker" | "validator" | null;
  model_slugs: Record<string, string>;
  /** Preset this session opened with. Omitted by older daemons. */
  preset?: string;
  /** Tier → hostname:port the client will call. Omitted by older daemons. */
  hosts?: Record<string, string>;
  /** Plan mode (brain lock). Omitted by older daemons — treat as off. */
  plan?: boolean;
}

export interface BoundaryUpdate extends DaemonEvent {
  type: "boundary_update";
  session_id: string;
  writable_paths: string[];
  allowed_commands: string[];
  network: string | string[];
  spend_usd: number;
  wall_clock_hours: number;
  max_iterations: number;
  source: string;
  // TD-1709: the workspace's attachment caps, so the composer refuses early
  // against real numbers. Optional because an older daemon does not send it.
  attachments?: AttachmentLimits;
}

export interface TurnComplete extends DaemonEvent {
  type: "turn_complete";
  session_id: string;
  tokens: number;
  cost: number;
  tier: "brain" | "worker" | "validator";
  duration: number;
  // TD-1008: a failed turn (provider/keychain error) is machine-visible so
  // notifications can key tailored copy off the typed cause.
  failed: boolean;
  error_code: string | null;
}

export interface SteeringReloaded extends DaemonEvent {
  type: "steering_reloaded";
  session_id: string;
  prefix_hash: string;
  // The whole cache prefix (base prompt + workspace root + steering), which
  // is what gets re-billed when the hash moves — not the cost of the user's
  // steering files. That is steering_tokens (TD-1810).
  prefix_tokens: number;
  steering_tokens: number;
  source_count: number;
}

export interface RuleActivated extends DaemonEvent {
  type: "rule_activated";
  session_id: string;
  rule_path: string;
}

export interface ImportedFile {
  path: string;
  depth: number;
  issue?: string | null;
}

export interface InstructionStackEntry {
  path: string;
  precedence: string;
  active: boolean;
  tokens: number;
  token_method: string;
  warnings: string[];
  subtree?: string | null;
  is_fallback: boolean;
  shadowed_path?: string | null;
  applies_to?: string[] | null;
  imports?: ImportedFile[];
}

export interface InstructionFileEntry {
  path: string;
  name: string;
  kind: "agents" | "claude" | "rule";
}

export interface ContextPinEntry {
  path: string;
  name: string;
  kind: "file" | "dir";
  lines: number;
}

export interface ContextPins extends DaemonEvent {
  type: "context_pins";
  workspace_path: string;
  pins: ContextPinEntry[];
  instruction_tokens?: number;
  memory_tokens?: number;
  pin_tokens?: number;
  capacity_cap?: number;
  dropped?: string[];
}

export interface InstructionFiles extends DaemonEvent {
  type: "instruction_files";
  workspace_path: string;
  files: InstructionFileEntry[];
  created?: string | null;
}

/** One slash command the composer can insert (TD-4501). */
export interface CommandEntry {
  name: string;
  description: string;
  source: "workspace" | "user" | "claude_workspace" | "claude_user";
  body: string;
  too_large?: boolean;
}

/** Reply to list_commands. Connection-scoped. */
export interface CommandList extends DaemonEvent {
  type: "command_list";
  workspace_path: string;
  commands: CommandEntry[];
}

export interface MemoryFileEntry {
  path: string;
  name: string;
  content: string;
}

export interface MemoryFiles extends DaemonEvent {
  type: "memory_files";
  workspace_path: string;
  files: MemoryFileEntry[];
}

/** Reply to get_charter / save_charter (TD-4002). Connection-scoped. */
export interface CharterDocument extends DaemonEvent {
  type: "charter";
  workspace_path: string;
  present: boolean;
  charter?: CharterFields | null;
  notes?: string;
}

/** Reply to start_autonomy (TD-4003 / TD-4101). Connection-scoped. */
export interface AutonomyStart extends DaemonEvent {
  type: "autonomy_start";
  workspace_path: string;
  ready: boolean;
  signed: boolean;
  error?: string | null;
  session_id?: string | null;
}

/** Wake-up card for an unattended run that stopped (TD-4303). Session-scoped. */
export interface AutonomySummary extends DaemonEvent {
  type: "autonomy_summary";
  session_id: string;
  reason: string;
  branch: string;
  ledger_path: string;
  changed: string[];
  refusals: string[];
  ledger_excerpt: string;
}

export interface MemoryFileDiff {
  action: "create" | "replace" | "delete";
  path: string;
  diff: string;
  before?: string | null;
  after?: string | null;
}

/** Distill produced diffs the user must accept, edit, or reject (TD-2401). */
export interface MemoryProposal extends DaemonEvent {
  type: "memory_proposal";
  session_id: string;
  proposal_id: string;
  files: MemoryFileDiff[];
}

export interface MemoryStackEntry {
  path: string;
  tokens: number;
  reason: "always-index" | "heading" | "embedding";
}

/** One discovered skill on the instruction stack (TD-4502). */
export interface SkillStackEntry {
  name: string;
  description: string;
  source: "workspace" | "user" | "claude_workspace" | "claude_user";
  loaded: boolean;
  tokens: number;
}

export interface InstructionStack extends DaemonEvent {
  type: "instruction_stack";
  session_id: string;
  sources: InstructionStackEntry[];
  total_tokens: number;
  token_method: string;
  // Cached prompt tokens the provider reported on the last main-loop call.
  // null when no figure was reported — never 0 on a missing field.
  last_cached_tokens?: number | null;
  // Whether a main-loop call has come back at all: tells "no turn yet"
  // apart from "the provider reports no cache figure" (TD-1811).
  cache_observed?: boolean;
  memory?: MemoryStackEntry[];
  memory_dropped?: MemoryStackEntry[];
  memory_placeholder?: boolean;
  skills?: SkillStackEntry[];
}

export interface SessionSummary {
  session_id: string;
  workspace_path: string;
  state:
    | "idle"
    | "running"
    | "awaiting_approval"
    | "paused"
    | "complete"
    | "failed"
    | "cancelled"
    | "interrupted";
  created_at: string;
  updated_at: string;
  event_count: number;
  /** Filed away by the user (TD-1715). The list stays complete — the rail,
   *  the recents menu, and the pane's auto-bind all read one event — and this
   *  is the flag "hidden from the default list" is rendered from. */
  archived: boolean;
  /** Pinned to the top of the rail (TD-3003). Machine-wide. */
  starred: boolean;
  /** Auto-title from the first non-empty user message (TD-3001). Null
   *  until then — the rail falls back to the short id. Additive. */
  title?: string | null;
}

export interface SessionList extends DaemonEvent {
  type: "session_list";
  seq: number;
  sessions: SessionSummary[];
}

export interface PolicyRules extends DaemonEvent {
  type: "policy_rules";
  seq: number;
  rules: PolicyRuleSummary[];
}

// TD-1101 first-run wizard: the daemon's reply to get_setup_state
// (and the ack for set_api_key / set_preset / set_skip_all_approvals).
// has_api_key is the
// first-run signal — probed from the keychain, never from disk.
// key_required (TD-1801) is false when the active preset runs entirely
// on loopback endpoints, which send no key at all.
export interface SetupState extends DaemonEvent {
  type: "setup_state";
  seq: number;
  has_api_key: boolean;
  key_required: boolean;
  presets: string[];
  active_preset: string;
  // TD-1703: the active preset's slug per tier, as the config file has it.
  // null where a loopback tier leaves its model to discovery (TD-1805) — the
  // settings screen shows that as discovered, never as an empty field, so a
  // save cannot pin a model the user left floating. Optional because an
  // older daemon does not send it.
  tier_slugs?: Record<string, string | null>;
  // TD-804: skip-all approvals. Additive, default off.
  skip_all_approvals?: boolean;
  // TD-2603: load ~/.tstdesk/memory/ after workspace memory. Additive, default off.
  load_global_memory?: boolean;
  // TD-2905: keep running when the window closes. Additive, default on.
  coworker_enabled?: boolean;
  // TD-3402: Screen-pane glow / agent cursor. Additive, default on.
  cu_glow?: boolean;
  cu_agent_cursor?: boolean;
  // TD-3402: host overlay on the real display. Additive, default off.
  cu_show_on_real_display?: boolean;
  pinned_workspaces?: string[];
  // TD-3603: Allow remote attach. Additive, default off.
  // remote_bind is the bound Tailscale address, never a token.
  remote_attach_enabled?: boolean;
  remote_bind?: string | null;
  // TD-1717: named keys. Additive; an older daemon does not send them.
  credentials?: CredentialSummary[];
  tier_credentials?: Record<string, string | null>;
  tier_loopback?: Record<string, boolean>;
  // TD-4403: listed MCP servers. Additive, default empty. Never a secret.
  mcp_servers?: McpServerSummary[];
}

export interface McpServerSummary {
  id: string;
  transport: "stdio" | "http";
  command: string[];
  url: string;
  enabled: boolean;
}

export interface CredentialSummary {
  id: string;
  name: string;
  stored: boolean;
  /** Host this key talks to (TD-1718). Null keeps the tier URL. */
  base_url?: string | null;
}

// TD-1101: reply to validate_api_key — a one-token live probe of the
// stored key. `detail` is actionable text; the key never appears.
export interface ApiKeyValidated extends DaemonEvent {
  type: "api_key_validated";
  seq: number;
  ok: boolean;
  detail: string;
}

// TD-1104 doctor: one row per check. `skip` means not applicable (no key
// to validate, no workspace open) — not a failure. `fix` is the concrete
// remedy, present exactly when status is "fail".
export interface DiagnosticCheck {
  name: string;
  status: "ok" | "fail" | "skip";
  detail: string;
  fix?: string | null;
}

export interface DiagnosticsReport extends DaemonEvent {
  type: "diagnostics_report";
  seq: number;
  checks: DiagnosticCheck[];
}

// TD-1706 usage view: one bucket's spend on one tier. `key` is a session
// id, an ISO day, or the ISO day the week opened on, per `bucket`.
// classifier_cost stays on its own field exactly as it does in the audit
// store — folding it into cost would make this disagree with the meter.
export interface UsageRollup {
  bucket: "session" | "day" | "week";
  key: string;
  tier: string;
  prompt_tokens: number;
  cached_prompt_tokens: number;
  completion_tokens: number;
  cost: number;
  classifier_cost: number;
}

export interface UsageReport extends DaemonEvent {
  type: "usage_report";
  seq: number;
  rows: UsageRollup[];
}

// TD-1706: where the export landed. `rows` is the model-call record count
// written, so the client can say "42 calls" rather than claim success
// over an empty file.
export interface UsageExported extends DaemonEvent {
  type: "usage_exported";
  seq: number;
  format: "jsonl" | "csv";
  path: string;
  rows: number;
}

/** Attach asked for a seq the on-disk window dropped (TD-2901).
 *  Connection-scoped: seq is fixed at 1 and does not belong to the
 *  session log. The client jumps lastSeq to earliest_seq-1 so replay
 *  from the kept window is not a false gap. */
export interface LogTrimmed extends DaemonEvent {
  type: "log_trimmed";
  session_id: string;
  requested_from_seq: number;
  earliest_seq: number;
}

/** One artifact on artifact_list (TD-3201). Path, not bytes. */
export interface ArtifactEntry {
  artifact_id: string;
  title: string;
  mime: string;
  path: string;
}

/** An artifact was recorded for this session (TD-3201). */
export interface ArtifactReady extends DaemonEvent {
  type: "artifact_ready";
  session_id: string;
  artifact_id: string;
  title: string;
  mime: string;
  path: string;
}

/** Response to list_artifacts (TD-3201). Connection-scoped. */
export interface ArtifactList extends DaemonEvent {
  type: "artifact_list";
  session_id: string;
  artifacts: ArtifactEntry[];
}

/** Response to open_artifact (TD-3201): metadata and path, not bytes. */
export interface Artifact extends DaemonEvent {
  type: "artifact";
  session_id: string;
  artifact_id: string;
  title: string;
  mime: string;
  path: string;
}

export interface Error extends DaemonEvent {
  type: "error";
  session_id?: string | null;
  code: string;
  message: string;
}

/** A browser screenshot written to the session dir (TD-1710). Path, not bytes. */
export interface ScreenFrame extends DaemonEvent {
  type: "screen_frame";
  session_id: string;
  path: string;
  mime: string;
  width?: number | null;
  height?: number | null;
  tool_call_id?: string | null;
}

/** Process-wide computer-use kill-switch (TD-3404). Connection-scoped, seq=1.
 *  `killed=true` also clears Screen-pane glow and cursor (TD-3402). */
export interface CuKillState extends DaemonEvent {
  type: "cu_kill_state";
  killed: boolean;
}

/** Computer-use episode open/close (TD-3407). Glow follows this tag. */
export interface CuSession extends DaemonEvent {
  type: "cu_session";
  session_id: string;
  active: boolean;
}

/** Reply to design_hit_test (TD-3403). Connection-scoped; seq is 1. */
export interface DesignHitBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface DesignHit extends DaemonEvent {
  type: "design_hit";
  session_id: string;
  x: number;
  y: number;
  xpath?: string | null;
  role?: string | null;
  attributes: Record<string, string>;
  box?: DesignHitBox | null;
  styles: Record<string, string>;
}

/** Computer-use OS permission / integrity report (TD-3302, TD-3303). Connection-scoped. */
export interface CuPermissions extends DaemonEvent {
  type: "cu_permissions";
  granted: boolean;
  screen_recording: boolean;
  accessibility: boolean;
  screen_recording_url: string;
  accessibility_url: string;
  first_run: boolean;
  platform: "macos" | "windows" | "linux";
  no_gate: string;
  uipi: string;
  secure_desktop: string;
  elevated: boolean;
  uipi_applies: boolean;
  secure_desktop_applies: boolean;
  session_type: string;
  wayland: string;
  wayland_applies: boolean;
  xtest: string;
  xtest_applies: boolean;
  no_display: string;
  no_display_applies: boolean;
}

export interface ContextCompacted extends DaemonEvent {
  type: "context_compacted";
  session_id: string;
  dropped_messages: number;
  kept_messages: number;
  tokens_before: number;
  tokens_after: number;
}

export interface TierSwitched extends DaemonEvent {
  type: "tier_switched";
  session_id: string;
  tier: "brain" | "worker" | "validator";
  previous?: "brain" | "worker" | "validator" | null;
}

/** One persisted job on job_list (TD-3805). */
export interface JobEntry {
  id: string;
  workspace: string;
  instruction: string;
  cadence: string | null;
  next_run: string | null;
  deliver_to: "window" | "slack" | "ntfy";
  paused: boolean;
}

/** Response to list_jobs / save_job / delete_job (TD-3805). Connection-scoped. */
export interface JobList extends DaemonEvent {
  type: "job_list";
  jobs: JobEntry[];
}

export type DaemonEventUnion =
  | Ready
  | SessionState
  | ConversationReset
  | UserTurn
  | AssistantDelta
  | AssistantReasoning
  | ToolCall
  | ToolResult
  | ShellOutput
  | ApprovalRequest
  | DecisionLogged
  | CheckpointNotice
  | VerifyResult
  | CostUpdate
  | BoundaryUpdate
  | TurnComplete
  | TierState
  | ContextCompacted
  | SteeringReloaded
  | RuleActivated
  | TierSwitched
  | InstructionStack
  | InstructionFiles
  | CommandList
  | ContextPins
  | MemoryFiles
  | CharterDocument
  | AutonomyStart
  | AutonomySummary
  | MemoryProposal
  | SessionList
  | PolicyRules
  | SetupState
  | ApiKeyValidated
  | DiagnosticsReport
  | UsageReport
  | UsageExported
  | LogTrimmed
  | ArtifactReady
  | ArtifactList
  | Artifact
  | Error
  | ScreenFrame
  | CuKillState
  | CuSession
  | DesignHit
  | CuPermissions
  | JobList;
