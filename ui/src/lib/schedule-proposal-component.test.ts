// @vitest-environment jsdom
//
// TD-5263: the schedule proposal card, mounted for real. It shows the whole
// instruction, when it runs and the workspace, with Save, Edit and Dismiss.
// Every button only sends the person's decision; the card leaves when the
// daemon logs how the proposal ended, not on the click.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const connection = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return { handlers, sent: [] as unknown[] };
});

vi.mock("./connection-status.svelte.js", () => ({
  onDaemonEvent: (handler: (event: unknown) => void) => {
    connection.handlers.add(handler);
    return () => connection.handlers.delete(handler);
  },
  sendToDaemon: (msg: unknown) => (connection.sent.push(msg), true),
}));

import ScheduleProposalBar from "./components/ScheduleProposalBar.svelte";
import fixtures from "./protocol-fixtures.json";
import type { ScheduleProposal, ScheduleProposalDecision } from "./protocol";
import { bindScheduleProposal } from "./schedule-proposal-store.svelte.js";
import { saveMessage, proposalFromEvent, whenCopy } from "./schedule-proposal";

const PROPOSAL = { ...(fixtures.schedule_proposal as ScheduleProposal), session_id: "s1" };
const SAVED = {
  ...(fixtures.schedule_proposal_decision as ScheduleProposalDecision),
  session_id: "s1",
};

function emit(event: unknown): void {
  for (const handler of connection.handlers) handler(event);
  flushSync();
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

beforeEach(() => {
  connection.sent.length = 0;
  bindScheduleProposal(null);
  bindScheduleProposal("s1");
  target = document.createElement("div");
  document.body.appendChild(target);
  component = mount(ScheduleProposalBar, { target });
  flushSync();
});

afterEach(() => {
  if (component !== null) unmount(component);
  component = null;
  target.remove();
});

function buttons(): string[] {
  return [...target.querySelectorAll("button")].map((b) => b.textContent?.trim() ?? "");
}

function click(label: string): void {
  const button = [...target.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
  if (button === undefined) throw new Error(`no ${label} button`);
  button.click();
  flushSync();
}

describe("schedule proposal card", () => {
  it("shows the instruction whole, when, and where, and saves only on Save", () => {
    emit(PROPOSAL);
    expect(target.textContent).toContain(PROPOSAL.instruction);
    expect(target.textContent).toContain("Weekdays at 8:00 AM (Europe/London)");
    expect(target.textContent).toContain(PROPOSAL.workspace);
    expect(target.textContent).toContain("Proposed by the agent in this chat");
    expect(buttons()).toEqual(["Save", "Edit", "Dismiss"]);
    expect(connection.sent).toEqual([]);
    click("Save");
    expect(connection.sent).toEqual([
      {
        type: "save_job",
        proposal_id: PROPOSAL.proposal_id,
        proposal_session_id: "s1",
        workspace: PROPOSAL.workspace,
        instruction: PROPOSAL.instruction,
        cadence: PROPOSAL.cadence,
        next_run: null,
        timezone: PROPOSAL.timezone,
        deliver_to: "window",
      },
    ]);
    // Still there until the daemon says it was saved.
    expect(buttons()).toContain("Save");
    emit(SAVED);
    expect(buttons()).toEqual([]);
  });

  it("sends the person's edit, and Dismiss schedules nothing", () => {
    emit(PROPOSAL);
    click("Edit");
    const box = target.querySelector("textarea") as HTMLTextAreaElement;
    box.value = "Only check the lockfile.";
    box.dispatchEvent(new Event("input", { bubbles: true }));
    flushSync();
    click("Save");
    expect((connection.sent[0] as { instruction: string }).instruction).toBe(
      "Only check the lockfile.",
    );
    click("Cancel");
    click("Dismiss");
    expect(connection.sent[1]).toEqual({
      type: "dismiss_schedule_proposal",
      session_id: "s1",
      proposal_id: PROPOSAL.proposal_id,
    });
  });

  it("a proposal the daemon no longer holds says so and only offers Dismiss", () => {
    emit(PROPOSAL);
    click("Save");
    emit({
      type: "error",
      seq: 1,
      code: "proposal_not_found",
      message: "That proposal is no longer open. Nothing was scheduled.",
      in_reply_to: "save_job",
    });
    expect(target.textContent).toContain("Nothing was scheduled");
    expect(buttons()).toEqual(["Dismiss"]);
    click("Dismiss");
    expect(connection.sent).toHaveLength(1);
    expect(buttons()).toEqual([]);
  });

  it("ignores another session's cards and a replayed one already decided", () => {
    emit({ ...PROPOSAL, session_id: "other" });
    expect(buttons()).toEqual([]);
    emit(PROPOSAL);
    emit({ ...SAVED, decision: "expired" });
    emit(PROPOSAL);
    expect(buttons()).toEqual([]);
  });
});

describe("schedule proposal model", () => {
  it("a one-off says when, and saves its time and no cadence", () => {
    const card = proposalFromEvent({
      ...PROPOSAL,
      cadence: null,
      timezone: null,
      next_run: "2030-01-02T09:00:00Z",
      next_runs: ["2030-01-02T09:00:00Z"],
    });
    expect(card).not.toBeNull();
    expect(whenCopy(card!, "UTC")).toMatch(/^Once, /);
    const msg = saveMessage(card!);
    expect(msg.cadence).toBeNull();
    expect(msg.next_run).toBe("2030-01-02T09:00:00Z");
  });

  it("a blank instruction is not a card", () => {
    expect(proposalFromEvent({ ...PROPOSAL, instruction: "  " })).toBeNull();
  });
});
