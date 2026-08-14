// Reactive approval-card store (TD-1007).
//
// The runes layer over the approval model. Subscribes to the daemon event
// stream: an `approval_request` adds a card, and the `tool_result` that
// resolves the call removes it (the timeline records the choice separately).
// `approve`/`deny` send the client messages through the connection.

import { alwaysAllowMessage, approvalFromEvent, approveMessage, denyMessage, type PendingApproval } from './approval';
import { onDaemonEvent, sendToDaemon } from './connection-status.svelte.js';

export const pending = $state<PendingApproval[]>([]);

// The array is mutated (never reassigned) so `pending` can stay an exported
// `$state` binding: Svelte 5 deep-proxies the array, so push/splice invalidate
// through it (mirrors timeline-store.svelte.ts / chat-store.svelte.ts).
onDaemonEvent((event) => {
  if (event.type === 'approval_request') {
    pending.push(approvalFromEvent(event));
  } else if (event.type === 'tool_result') {
    const idx = pending.findIndex((p) => p.toolCallId === event.tool_call_id);
    if (idx !== -1) pending.splice(idx, 1);
  }
});

/** Approve the pending call. */
export function approve(approval: PendingApproval): void {
  sendToDaemon(approveMessage(approval));
}

/** Deny the pending call, optionally passing a note back to the model. */
export function deny(approval: PendingApproval, note?: string): void {
  sendToDaemon(denyMessage(approval, note));
}

/** Always allow this call in the workspace, then resolve it as approved. */
export function alwaysAllow(approval: PendingApproval): void {
  sendToDaemon(alwaysAllowMessage(approval));
}
