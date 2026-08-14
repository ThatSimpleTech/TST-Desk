// Reactive approval-card store (TD-1007).
//
// The runes layer over the approval model. Subscribes to the daemon event
// stream: an `approval_request` adds a card, and the `tool_result` that
// resolves the call removes it (the timeline records the choice separately).
// `approve`/`deny` send the client messages through the connection.

import { approvalFromEvent, approveMessage, denyMessage, type PendingApproval } from './approval';
import { onEvent, send } from './connection-status';

export let pending = $state<PendingApproval[]>([]);

// The list is reassigned (not mutated) so Svelte's reactivity invalidates on
// each add/remove (mirrors timeline-store.ts).
onEvent((event) => {
  if (event.type === 'approval_request') {
    pending = [...pending, approvalFromEvent(event)];
  } else if (event.type === 'tool_result') {
    pending = pending.filter((p) => p.toolCallId !== event.tool_call_id);
  }
});

/** Approve the pending call. */
export function approve(approval: PendingApproval): void {
  send(approveMessage(approval));
}

/** Deny the pending call, optionally passing a note back to the model. */
export function deny(approval: PendingApproval, note?: string): void {
  send(denyMessage(approval, note));
}
