// Schedule-proposal card model (TD-5263).
//
// Pure, DOM-free mapping of a `schedule_proposal` event into what the card
// shows, plus the save and dismiss messages it sends. The agent cannot
// schedule anything: only the person's Save, sent from this window, makes
// the daemon create the job, and the daemon (not this message) marks it as
// agent-proposed and aims it at the proposing session's project.

import type { DismissScheduleProposal, SaveJob, ScheduleProposal } from "./protocol";
import { formatLocal, humanizeCadence } from "./scheduled";

/** The verbs whose errors belong to a schedule card. */
export const SCHEDULE_VERBS: ReadonlySet<string> = new Set([
  "save_job",
  "dismiss_schedule_proposal",
]);

export interface PendingScheduleProposal {
  sessionId: string;
  proposalId: string;
  workspace: string;
  /** Shown whole, never summarized. */
  instruction: string;
  cadence: string | null;
  nextRun: string | null;
  timezone: string | null;
  nextRuns: string[];
  notice: string | null;
  /** False once the daemon says it no longer holds the proposal. */
  open: boolean;
}

/** What the person may change before saving. */
export interface ScheduleEdit {
  instruction: string;
  cadence: string;
}

export function proposalFromEvent(event: ScheduleProposal): PendingScheduleProposal | null {
  if (event.instruction.trim() === "" || event.workspace === "") return null;
  return {
    sessionId: event.session_id,
    proposalId: event.proposal_id,
    workspace: event.workspace,
    instruction: event.instruction,
    cadence: event.cadence ?? null,
    nextRun: event.next_run ?? null,
    timezone: event.timezone ?? null,
    nextRuns: event.next_runs ?? [],
    notice: null,
    open: true,
  };
}

/** When it runs, in words: the cadence, or the one time for a one-off. */
export function whenCopy(p: PendingScheduleProposal, timeZone?: string): string {
  if (p.cadence !== null) {
    const words = humanizeCadence(p.cadence, p.timezone);
    return p.timezone ? `${words} (${p.timezone})` : words;
  }
  return p.nextRun === null ? "Not set" : `Once, ${formatLocal(p.nextRun, timeZone)}`;
}

/** The next few runs in the viewer's own zone. */
export function nextRunsCopy(p: PendingScheduleProposal, timeZone?: string): string[] {
  return p.nextRuns.map((iso) => formatLocal(iso, timeZone));
}

/** Save as proposed, or with the person's edit. Delivered to this window. */
export function saveMessage(p: PendingScheduleProposal, edit?: ScheduleEdit): SaveJob {
  const cadence = edit === undefined ? p.cadence : edit.cadence.trim() || null;
  return {
    type: "save_job",
    proposal_id: p.proposalId,
    proposal_session_id: p.sessionId,
    workspace: p.workspace,
    instruction: edit === undefined ? p.instruction : edit.instruction,
    cadence,
    next_run: cadence === null ? p.nextRun : null,
    timezone: p.timezone,
    deliver_to: "window",
  };
}

export function dismissMessage(p: PendingScheduleProposal): DismissScheduleProposal {
  return { type: "dismiss_schedule_proposal", session_id: p.sessionId, proposal_id: p.proposalId };
}
