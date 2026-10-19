// Reactive steering-proposal store (TD-5140).
//
// Same bind-and-clear contract as the memory and approval cards (TD-1014):
// a switch drops what the previous session left, and the bind's attach
// replay rebuilds every proposal still open. Unlike memory, a decision is
// an event in the log (`steering_decision`), so a card leaves when the
// daemon says it was decided, not when the click is sent: a stale file
// swaps the card for a fresh diff (`replaces`), and a refusal stays on the
// card as a notice.

import {
  acceptMessage,
  editMessage,
  proposalFromEvent,
  rejectMessage,
  STEERING_VERBS,
  type PendingSteeringProposal,
} from "./steering-proposal";
import { onDaemonEvent, sendToDaemon } from "./connection-status.svelte.js";

export const pending = $state<PendingSteeringProposal[]>([]);

/** The session the cards belong to. Null = unbound. */
let boundSessionId: string | null = null;
/** Proposals the log closed (decided or replaced); a replay must not revive them. */
const closed = new Set<string>();
/** The card the last decision was sent from: a refusal comes back without an id. */
let lastSent: string | null = null;

const NOT_SENT =
  "Not sent: the daemon is not connected, or this session runs on another computer.";

onDaemonEvent((event) => {
  if (event.type === "steering_proposal") {
    if (event.session_id !== boundSessionId) return;
    if (event.replaces) close(event.replaces);
    if (closed.has(event.proposal_id) || indexOf(event.proposal_id) !== -1) return;
    const card = proposalFromEvent(event);
    if (card !== null) pending.push(card);
    return;
  }
  if (event.type === "steering_decision") {
    if (event.session_id !== boundSessionId) return;
    close(event.proposal_id);
    return;
  }
  if (event.type === "error" && STEERING_VERBS.has(event.in_reply_to ?? "")) {
    if (event.session_id != null && event.session_id !== boundSessionId) return;
    const target = lastSent;
    lastSent = null;
    if (target === null) return;
    if (event.code === "no_steering_proposal") {
      close(target);
      return;
    }
    // A stale file already swapped the card for a fresh one.
    if (event.code !== "steering_proposal_stale") setNotice(target, event.message);
  }
});

/** Point the cards at a session, or at nothing (TD-1014). Same session: no-op. */
export function bindSteeringProposal(sessionId: string | null): void {
  if (sessionId === boundSessionId) return;
  boundSessionId = sessionId;
  closed.clear();
  lastSent = null;
  pending.splice(0, pending.length);
}

function indexOf(proposalId: string): number {
  return pending.findIndex((p) => p.proposalId === proposalId);
}

function close(proposalId: string): void {
  closed.add(proposalId);
  const idx = indexOf(proposalId);
  if (idx !== -1) pending.splice(idx, 1);
}

function setNotice(proposalId: string, notice: string | null): void {
  const card = pending.find((p) => p.proposalId === proposalId);
  if (card !== undefined) card.notice = notice;
}

function send(proposal: PendingSteeringProposal, msg: Parameters<typeof sendToDaemon>[0]): void {
  if (indexOf(proposal.proposalId) === -1) return;
  setNotice(proposal.proposalId, null);
  if (sendToDaemon(msg)) {
    lastSent = proposal.proposalId;
  } else {
    setNotice(proposal.proposalId, NOT_SENT);
  }
}

/** Accept: the daemon writes the proposed text. The card leaves on the decision. */
export function accept(proposal: PendingSteeringProposal): void {
  send(proposal, acceptMessage(proposal));
}

/** Edit, then Save: the daemon writes the user's own text. */
export function saveEdit(proposal: PendingSteeringProposal, content: string): void {
  send(proposal, editMessage(proposal, content));
}

/** Reject: writes nothing; the daemon logs the decision. */
export function reject(proposal: PendingSteeringProposal): void {
  send(proposal, rejectMessage(proposal));
}
