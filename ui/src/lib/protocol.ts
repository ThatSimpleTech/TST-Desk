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

export interface Resume extends ClientMessage {
  type: "resume";
  session_id: string;
}

export interface Cancel extends ClientMessage {
  type: "cancel";
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

export interface GetInstructionStack extends ClientMessage {
  type: "get_instruction_stack";
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

// ── Onboarding (TD-1101 first-run wizard) ────────────────────────────

export interface GetSetupState extends ClientMessage {
  type: "get_setup_state";
}

export interface SetApiKey extends ClientMessage {
  type: "set_api_key";
  api_key: string;
}

// TD-1102: key removable from settings. Acked with a fresh setup_state
// (has_api_key flips false), same pattern as set_api_key.
export interface DeleteApiKey extends ClientMessage {
  type: "delete_api_key";
  provider?: string;
}

export interface ValidateApiKey extends ClientMessage {
  type: "validate_api_key";
  // TD-1106: when present, the typed key is checked directly instead of
  // the stored one — validation never depends on keychain state.
  api_key?: string | null;
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

export type ClientMessageUnion =
  | Hello
  | OpenWorkspace
  | UserMessage
  | Approve
  | Deny
  | AlwaysAllow
  | ListPolicyRules
  | RevokePolicyRule
  | Resume
  | Cancel
  | Attach
  | Detach
  | SetTier
  | GetInstructionStack
  | Shutdown
  | ListSessions
  | NewSession
  | ArchiveSession
  | DeleteSession
  | MoveSession
  | GetSetupState
  | SetApiKey
  | DeleteApiKey
  | ValidateApiKey
  | SetPreset
  | SetTierSlug
  | RunDiagnostics
  | GetUsage
  | ExportUsage;

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

/** Active tier + configured slugs (TD-1006). */
export interface TierState extends DaemonEvent {
  type: "tier_state";
  session_id: string;
  tier: "brain" | "worker" | "validator";
  override: "brain" | "worker" | "validator" | null;
  model_slugs: Record<string, string>;
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
// (and the ack for set_api_key / set_preset). has_api_key is the
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

export interface Error extends DaemonEvent {
  type: "error";
  session_id?: string | null;
  code: string;
  message: string;
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

export type DaemonEventUnion =
  | Ready
  | SessionState
  | AssistantDelta
  | AssistantReasoning
  | ToolCall
  | ToolResult
  | ShellOutput
  | ApprovalRequest
  | DecisionLogged
  | CheckpointNotice
  | CostUpdate
  | BoundaryUpdate
  | TurnComplete
  | TierState
  | ContextCompacted
  | SteeringReloaded
  | RuleActivated
  | TierSwitched
  | InstructionStack
  | SessionList
  | PolicyRules
  | SetupState
  | ApiKeyValidated
  | DiagnosticsReport
  | UsageReport
  | UsageExported
  | Error;