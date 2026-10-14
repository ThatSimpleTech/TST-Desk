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
//
// TD-5015: the hub never queues a decision for another computer. Unless the
// lane there is open it answers `machine_unreachable` at once, so a card for
// a bridged session is decided only while its computer is connected, and one
// already sent comes back if the hub reports it never got there.

import { alwaysAllowMessage, approvalFromEvent, approveMessage, denyMessage, type PendingApproval } from './approval';
import { isBridged } from './bridged-id';
import { onDaemonEvent, sendToDaemon } from './connection-status.svelte.js';
import { sessionMachineUp } from './machines.svelte.js';
import type { ClientMessageUnion } from './protocol';

export const pending = $state<PendingApproval[]>([]);

/** The session the footer is showing cards for. Null = unbound, show none. */
let boundSessionId: string | null = null;

/** Cards for another computer's session that left on a send and await the
 *  `tool_result` that proves the decision arrived, by tool call id. */
const sentRemote = new Map<string, PendingApproval>();

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
    sentRemote.delete(event.tool_call_id);
    dismiss(event.tool_call_id);
  } else if (event.type === 'error' && event.code === 'machine_unreachable') {
    if (boundSessionId === null || event.session_id !== boundSessionId) return;
    restoreUndelivered();
  }
});

/** Put back every card whose decision the hub could not deliver. */
function restoreUndelivered(): void {
  for (const approval of sentRemote.values()) {
    if (!isPending(approval.toolCallId)) pending.push(approval);
  }
  sentRemote.clear();
}

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
  sentRemote.clear();
}

function dismiss(toolCallId: string): void {
  const idx = pending.findIndex((p) => p.toolCallId === toolCallId);
  if (idx !== -1) pending.splice(idx, 1);
}

function isPending(toolCallId: string): boolean {
  return pending.some((p) => p.toolCallId === toolCallId);
}

/** False while the card's session is on a computer that isn't connected:
 *  the hub would refuse the decision rather than queue it. */
export function canDecide(approval: PendingApproval): boolean {
  return sessionMachineUp(approval.sessionId);
}

/** Send one decision. The card leaves when the send lands, not when
 *  `tool_result` arrives — a second click on a parked card was
 *  `no_pending_approval`. */
function decide(approval: PendingApproval, msg: ClientMessageUnion): void {
  if (!isPending(approval.toolCallId) || !canDecide(approval)) return;
  if (!sendToDaemon(msg)) return;
  if (isBridged(approval.sessionId)) sentRemote.set(approval.toolCallId, approval);
  dismiss(approval.toolCallId);
}

/** Approve the pending call. */
export function approve(approval: PendingApproval): void {
  decide(approval, approveMessage(approval));
}

/** Deny the pending call, optionally passing a note back to the model. */
export function deny(approval: PendingApproval, note?: string): void {
  decide(approval, denyMessage(approval, note));
}

/** Always allow this call in the workspace, then resolve it as approved. */
export function alwaysAllow(approval: PendingApproval): void {
  decide(approval, alwaysAllowMessage(approval));
}
