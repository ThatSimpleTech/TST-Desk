// Reactive approval-card store (TD-1007, TD-1014, TD-3702).
//
// The runes layer over the approval model. Subscribes to the daemon event
// stream: an `approval_request` adds a card, and the `tool_result` that
// resolves the call removes it (the timeline records the choice separately).
// `approve`/`deny` send the client messages through the connection.
// A browser attach uses this same store — there is no second remote list.
//
// Cards are scoped to the bound session the same way the timeline and the
// decisions pane are (TD-1009 / TD-1203). A leftover card from a previous
// session, or a replayed request whose call the daemon already resolved,
// is what produced `no_pending_approval` on Approve.

import { alwaysAllowMessage, approvalFromEvent, approveMessage, denyMessage, type PendingApproval } from './approval';
import { onDaemonEvent, sendToDaemon } from './connection-status.svelte.js';

export const pending = $state<PendingApproval[]>([]);

/** The session the footer is showing cards for. Null = unbound, show none. */
let boundSessionId: string | null = null;

// The array is mutated (never reassigned) so `pending` can stay an exported
// `$state` binding: Svelte 5 deep-proxies the array, so push/splice invalidate
// through it (mirrors timeline-store.svelte.ts / chat-store.svelte.ts).
onDaemonEvent((event) => {
  if (event.type === 'approval_request') {
    if (boundSessionId === null || event.session_id !== boundSessionId) return;
    if (pending.some((p) => p.toolCallId === event.tool_call_id)) return;
    pending.push(approvalFromEvent(event));
  } else if (event.type === 'tool_result') {
    if (boundSessionId === null || event.session_id !== boundSessionId) return;
    dismiss(event.tool_call_id);
  }
});

/** Point the footer at a session, or at nothing (TD-1014).
 *
 * Same contract as the timeline and the decisions pane: a switch drops
 * what the previous session left, and the bind's attach replay rebuilds
 * anything still pending. Re-binding the session already shown is a
 * no-op so a reconnect that replays only the gap does not empty the
 * footer with nothing coming back to refill it.
 */
export function bindApprovals(sessionId: string | null): void {
  if (sessionId === boundSessionId) return;
  boundSessionId = sessionId;
  pending.splice(0, pending.length);
}

function dismiss(toolCallId: string): void {
  const idx = pending.findIndex((p) => p.toolCallId === toolCallId);
  if (idx !== -1) pending.splice(idx, 1);
}

function isPending(toolCallId: string): boolean {
  return pending.some((p) => p.toolCallId === toolCallId);
}

/** Approve the pending call. The card leaves when the send lands, not
 *  when `tool_result` arrives — a second click on a parked card was
 *  `no_pending_approval`. */
export function approve(approval: PendingApproval): void {
  if (!isPending(approval.toolCallId)) return;
  if (sendToDaemon(approveMessage(approval))) dismiss(approval.toolCallId);
}

/** Deny the pending call, optionally passing a note back to the model. */
export function deny(approval: PendingApproval, note?: string): void {
  if (!isPending(approval.toolCallId)) return;
  if (sendToDaemon(denyMessage(approval, note))) dismiss(approval.toolCallId);
}

/** Always allow this call in the workspace, then resolve it as approved. */
export function alwaysAllow(approval: PendingApproval): void {
  if (!isPending(approval.toolCallId)) return;
  if (sendToDaemon(alwaysAllowMessage(approval))) dismiss(approval.toolCallId);
}
