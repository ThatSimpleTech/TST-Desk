// Reactive schedule-proposal store (TD-5263).
//
// Same bind-and-clear contract as the steering cards (TD-5140): a switch
// drops the previous session's cards and the attach replay rebuilds the
// open ones. A card leaves when the daemon logs how it ended
// (`schedule_proposal_decision`: saved, dismissed, or expired after a
// restart), never on the click, so a refused save stays on the card.

import {
  dismissMessage,
  proposalFromEvent,
  saveMessage,
  SCHEDULE_VERBS,
  type PendingScheduleProposal,
  type ScheduleEdit,
} from "./schedule-proposal";
import { onDaemonEvent, sendToDaemon } from "./connection-status.svelte.js";

export const pending = $state<PendingScheduleProposal[]>([]);

let boundSessionId: string | null = null;
const closed = new Set<string>();
/** The card the last Save or Dismiss came from; errors carry no proposal id. */
let lastSent: string | null = null;

const NOT_SENT =
  "Not sent: the daemon is not connected, or this session runs on another computer.";

onDaemonEvent((event) => {
  if (event.type === "schedule_proposal") {
    if (event.session_id !== boundSessionId) return;
    if (closed.has(event.proposal_id) || indexOf(event.proposal_id) !== -1) return;
    const card = proposalFromEvent(event);
    if (card !== null) pending.push(card);
    return;
  }
  if (event.type === "schedule_proposal_decision") {
    if (event.session_id !== boundSessionId) return;
    close(event.proposal_id);
    if (lastSent === event.proposal_id) lastSent = null;
    return;
  }
  if (event.type === "error" && lastSent !== null && !event.machine) {
    const ours = SCHEDULE_VERBS.has(event.in_reply_to ?? "") || event.code === "job_invalid";
    if (!ours) return;
    const card = pending.find((p) => p.proposalId === lastSent);
    lastSent = null;
    if (card === undefined) return;
    card.notice = event.message;
    if (event.code === "proposal_not_found") card.open = false;
  }
});

/** Point the cards at a session, or at nothing. Same session: no-op. */
export function bindScheduleProposal(sessionId: string | null): void {
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

function send(proposal: PendingScheduleProposal, msg: Parameters<typeof sendToDaemon>[0]): void {
  const card = pending.find((p) => p.proposalId === proposal.proposalId);
  if (card === undefined || !card.open) return;
  card.notice = null;
  if (sendToDaemon(msg)) lastSent = proposal.proposalId;
  else card.notice = NOT_SENT;
}

/** Save: the daemon creates the job. The card leaves on the decision. */
export function save(proposal: PendingScheduleProposal, edit?: ScheduleEdit): void {
  send(proposal, saveMessage(proposal, edit));
}

/** Dismiss: nothing is scheduled; the daemon logs it. */
export function dismissProposal(proposal: PendingScheduleProposal): void {
  send(proposal, dismissMessage(proposal));
}

/** Close a card the daemon no longer holds. Sends nothing. */
export function forget(proposal: PendingScheduleProposal): void {
  close(proposal.proposalId);
}
