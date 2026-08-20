// Memory-proposal card model (TD-2402).
//
// Pure, DOM-free mapping of a `memory_proposal` event into the shape the
// card renders, plus the accept/reject messages it sends back. Kept free
// of runes so it unit-tests under vitest's node environment. The runes
// layer lives in memory-proposal-store.svelte.ts.

import type { MemoryAccept, MemoryFileDiff, MemoryProposal, MemoryReject } from "./protocol";

/** A pending distill proposal, reduced to what the card shows. */
export interface PendingMemoryProposal {
  sessionId: string;
  proposalId: string;
  files: MemoryFileDiff[];
}

/** Map a memory_proposal event to the card's pending shape. */
export function proposalFromEvent(event: MemoryProposal): PendingMemoryProposal {
  return {
    sessionId: event.session_id,
    proposalId: event.proposal_id,
    files: event.files,
  };
}

/** The client message that accepts the parked proposal as-is (TD-2303 writes). */
export function acceptMessage(proposal: PendingMemoryProposal): MemoryAccept {
  return {
    type: "memory_accept",
    session_id: proposal.sessionId,
    proposal_id: proposal.proposalId,
  };
}

/** The client message that rejects the proposal. Writes nothing. */
export function rejectMessage(proposal: PendingMemoryProposal): MemoryReject {
  return {
    type: "memory_reject",
    session_id: proposal.sessionId,
    proposal_id: proposal.proposalId,
  };
}
