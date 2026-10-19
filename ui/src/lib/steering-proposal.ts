// Steering-proposal card model (TD-5140).
//
// Pure, DOM-free mapping of a `steering_proposal` event into the shape the
// card renders, plus the accept / edit / reject messages it sends back.
// The agent never writes a steering file: only these messages, sent by the
// user's click, make the daemon write one. The runes layer lives in
// steering-proposal-store.svelte.ts.

import type {
  SteeringAccept,
  SteeringEdit,
  SteeringFileDiff,
  SteeringProposal,
  SteeringReject,
} from "./protocol";

/** The verbs whose errors belong to a steering card. */
export const STEERING_VERBS: ReadonlySet<string> = new Set([
  "steering_accept",
  "steering_edit",
  "steering_reject",
]);

/** Shown on a card that replaced a stale proposal (the daemon's own words). */
export const FRESH_DIFF_NOTICE =
  "The file changed since this was proposed. This is a fresh diff against the file as it is now.";

/** A pending steering proposal, reduced to what the card shows. */
export interface PendingSteeringProposal {
  sessionId: string;
  proposalId: string;
  file: SteeringFileDiff;
  /** Parked by an unattended run for its summary. */
  autonomy: boolean;
  /** The daemon's last word on this card (a refusal, or a fresh diff). */
  notice: string | null;
}

/** Map a steering_proposal event to the card's pending shape. Null if it has no file. */
export function proposalFromEvent(event: SteeringProposal): PendingSteeringProposal | null {
  const file = event.files[0];
  if (file === undefined) return null;
  return {
    sessionId: event.session_id,
    proposalId: event.proposal_id,
    file,
    autonomy: event.autonomy === true,
    notice: event.replaces ? FRESH_DIFF_NOTICE : null,
  };
}

export function acceptMessage(proposal: PendingSteeringProposal): SteeringAccept {
  return {
    type: "steering_accept",
    session_id: proposal.sessionId,
    proposal_id: proposal.proposalId,
  };
}

export function editMessage(proposal: PendingSteeringProposal, content: string): SteeringEdit {
  return {
    type: "steering_edit",
    session_id: proposal.sessionId,
    proposal_id: proposal.proposalId,
    content,
  };
}

export function rejectMessage(proposal: PendingSteeringProposal): SteeringReject {
  return {
    type: "steering_reject",
    session_id: proposal.sessionId,
    proposal_id: proposal.proposalId,
  };
}

/** The card's title for one file: "Create AGENTS.md" or "Change .tst/rules/x.md". */
export function actionTitle(file: SteeringFileDiff): string {
  return `${file.action === "create" ? "Create" : "Change"} ${file.path}`;
}
