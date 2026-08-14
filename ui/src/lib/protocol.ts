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

export type ClientMessageUnion =
  | Hello
  | OpenWorkspace
  | UserMessage
  | Approve
  | Deny
  | Cancel
  | Attach
  | Detach
  | SetTier
  | GetInstructionStack;

// ── Daemon → Client ───────────────────────────────────────────────────

export interface Ready extends DaemonEvent {
  type: "ready";
  version: string;
  protocol_version: number;
}

export interface SessionState extends DaemonEvent {
  type: "session_state";
  session_id: string;
  state: "idle" | "running" | "awaiting_approval" | "complete" | "failed" | "cancelled";
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
  diff?: string | null;
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
}

export interface DecisionLogged extends DaemonEvent {
  type: "decision_logged";
  session_id: string;
  decision_class: "A" | "B" | "C";
  what: string;
  why: string;
  commit: string;
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
}

export interface Error extends DaemonEvent {
  type: "error";
  session_id?: string | null;
  code: string;
  message: string;
}

export type DaemonEventUnion =
  | Ready
  | SessionState
  | AssistantDelta
  | ToolCall
  | ToolResult
  | ApprovalRequest
  | DecisionLogged
  | CheckpointNotice
  | CostUpdate
  | BoundaryUpdate
  | TurnComplete
  | Error;