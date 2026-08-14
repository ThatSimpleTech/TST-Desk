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

export interface UserMessage extends ClientMessage {
  type: "user_message";
  session_id: string;
  content: string;
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

// ── Onboarding (TD-1101 first-run wizard) ────────────────────────────

export interface GetSetupState extends ClientMessage {
  type: "get_setup_state";
}

export interface SetApiKey extends ClientMessage {
  type: "set_api_key";
  api_key: string;
}

export interface ValidateApiKey extends ClientMessage {
  type: "validate_api_key";
}

export interface SetPreset extends ClientMessage {
  type: "set_preset";
  name: string;
}

// ── Diagnostics (TD-1104 doctor) ─────────────────────────────────────

export interface RunDiagnostics extends ClientMessage {
  type: "run_diagnostics";
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
  | GetSetupState
  | SetApiKey
  | ValidateApiKey
  | SetPreset
  | RunDiagnostics;

// ── Daemon → Client ───────────────────────────────────────────────────

/** Out-of-band handshake reply to `hello`. Not a sequenced daemon event. */
export interface HelloAck {
  type: "hello_ack";
  version: number;
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
  prefix_tokens: number;
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
  last_cached_tokens?: number | null;
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
export interface SetupState extends DaemonEvent {
  type: "setup_state";
  seq: number;
  has_api_key: boolean;
  presets: string[];
  active_preset: string;
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
  | Error;