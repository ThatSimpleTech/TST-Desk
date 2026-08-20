// Reactive memory-proposal store (TD-2402).
//
// Same bind-and-clear contract as approval cards (TD-1014): a switch
// drops what the previous session left, and the bind's attach replay
// rebuilds a still-parked proposal. The daemon parks one proposal per
// session, so a newer event replaces the card. Accept/reject dismiss
// on send — there is no resolving event in the log.

import { acceptMessage, proposalFromEvent, rejectMessage, type PendingMemoryProposal } from "./memory-proposal";
import { onDaemonEvent, sendToDaemon } from "./connection-status.svelte.js";

export const pending = $state<PendingMemoryProposal[]>([]);

/** The session the footer is showing a proposal for. Null = unbound. */
let boundSessionId: string | null = null;

onDaemonEvent((event) => {
  if (event.type === "memory_proposal") {
    if (boundSessionId === null || event.session_id !== boundSessionId) return;
    if (pending.some((p) => p.proposalId === event.proposal_id)) return;
    pending.splice(0, pending.length);
    pending.push(proposalFromEvent(event));
    return;
  }
  if (event.type === "error" && event.code === "no_memory_proposal") {
    if (boundSessionId === null || event.session_id !== boundSessionId) return;
    pending.splice(0, pending.length);
  }
});

/** Point the footer at a session, or at nothing (TD-1014).
 *
 * Re-binding the session already shown is a no-op so a reconnect that
 * replays only the gap does not empty the footer with nothing coming
 * back to refill it.
 */
export function bindMemoryProposal(sessionId: string | null): void {
  if (sessionId === boundSessionId) return;
  boundSessionId = sessionId;
  pending.splice(0, pending.length);
}

function isPending(proposalId: string): boolean {
  return pending.some((p) => p.proposalId === proposalId);
}

function dismiss(proposalId: string): void {
  const idx = pending.findIndex((p) => p.proposalId === proposalId);
  if (idx !== -1) pending.splice(idx, 1);
}

/** Accept the parked proposal. The card leaves when the send lands;
 *  the daemon writes through the memory store (TD-2303 / TD-2104). */
export function accept(proposal: PendingMemoryProposal): void {
  if (!isPending(proposal.proposalId)) return;
  if (sendToDaemon(acceptMessage(proposal))) dismiss(proposal.proposalId);
}

/** Reject the parked proposal. Writes nothing. */
export function reject(proposal: PendingMemoryProposal): void {
  if (!isPending(proposal.proposalId)) return;
  if (sendToDaemon(rejectMessage(proposal))) dismiss(proposal.proposalId);
}
