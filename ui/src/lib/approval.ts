// Approval-card model (TD-1007).
//
// Pure, DOM-free transformation of an `approval_request` event into the
// shape the card renders, plus the client messages the card sends back.
// Kept free of runes and Tauri imports so it unit-tests under vitest's node
// environment (like timeline.ts). The runes layer lives in approval-store.svelte.ts.

import type { AlwaysAllow, ApprovalRequest, Approve, Deny, PolicyRuleSummary } from './protocol';

/** A pending approval, reduced from the daemon event to what the card shows. */
export interface PendingApproval {
  sessionId: string;
  toolCallId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  decisionClass: 'A' | 'B' | 'C';
  summary: string;
  reason: string;
  /** Rule the daemon proposes to auto-approve this call forever (TD-803), or null. */
  proposedAlwaysAllow: PolicyRuleSummary | null;
}

/** Map an approval_request event to the card's pending-approval shape. */
export function approvalFromEvent(event: ApprovalRequest): PendingApproval {
  return {
    sessionId: event.session_id,
    toolCallId: event.tool_call_id,
    toolName: event.tool_name,
    arguments: event.arguments,
    decisionClass: event.decision_class,
    summary: event.summary,
    reason: event.reason,
    proposedAlwaysAllow: event.proposed_always_allow ?? null,
  };
}

/**
 * Class-C calls are the dangerous ones (the wall — policy can never run
 * them automatically); class B merely requires approval. Class A never
 * reaches a card at all.
 */
export function isDangerous(approval: PendingApproval): boolean {
  return approval.decisionClass === 'C';
}

/** The client message that approves the call. */
export function approveMessage(approval: PendingApproval): Approve {
  return {
    type: 'approve',
    session_id: approval.sessionId,
    tool_call_id: approval.toolCallId,
  };
}

/** The client message that denies the call, with an optional note for the model. */
export function denyMessage(approval: PendingApproval, reason?: string): Deny {
  return {
    type: 'deny',
    session_id: approval.sessionId,
    tool_call_id: approval.toolCallId,
    reason: reason?.trim() ? reason : null,
  };
}

/**
 * Whether the card may offer "Always allow in this workspace". The daemon only
 * attaches `proposed_always_allow` when a rule can be generated — which it
 * refuses for class-C calls (the wall). Guarding on both keeps the intent
 * explicit and the button absent for anything dangerous (AC #2/#4).
 */
export function isAlwaysAllowable(approval: PendingApproval): boolean {
  return approval.decisionClass !== 'C' && approval.proposedAlwaysAllow !== null;
}

/** The client message that records an always-allow rule and resolves the call. */
export function alwaysAllowMessage(approval: PendingApproval): AlwaysAllow {
  return {
    type: 'always_allow',
    session_id: approval.sessionId,
    tool_call_id: approval.toolCallId,
  };
}

/** Human-readable description of the proposed rule, for the button's tooltip. */
export function alwaysAllowLabel(approval: PendingApproval): string {
  const rule = approval.proposedAlwaysAllow;
  if (!rule) return '';
  return `${rule.tool}(${rule.args}) → ${rule.effect}`;
}
