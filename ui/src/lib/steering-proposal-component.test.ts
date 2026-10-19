// @vitest-environment jsdom
//
// TD-5140: the steering proposal card, mounted for real. It shows the diff
// with Accept, Edit and Reject; Edit opens the text with Save and Cancel.
// Every button only sends the user's decision: nothing on the card writes.

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

import SteeringProposalBar from "./components/SteeringProposalBar.svelte";
import fixtures from "./protocol-fixtures.json";
import type { Error as DaemonError, SteeringDecision, SteeringProposal } from "./protocol";
import { bindSteeringProposal } from "./steering-proposal-store.svelte.js";

const PROPOSAL = { ...(fixtures.steering_proposal as SteeringProposal), session_id: "s1" };

function emit(event: SteeringProposal | SteeringDecision | DaemonError): void {
  for (const handler of connection.handlers) handler(event);
  flushSync();
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

beforeEach(() => {
  connection.sent.length = 0;
  bindSteeringProposal(null);
  bindSteeringProposal("s1");
  target = document.createElement("div");
  document.body.appendChild(target);
  component = mount(SteeringProposalBar, { target });
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
  const button = [...target.querySelectorAll("button")].find(
    (b) => b.textContent?.trim() === label,
  );
  expect(button, label).toBeDefined();
  button!.click();
  flushSync();
}

describe("SteeringProposalCard", () => {
  it("shows nothing until there is a proposal", () => {
    expect(target.querySelector("article")).toBeNull();
  });

  it("shows the diff with Accept, Edit and Reject", () => {
    emit(PROPOSAL);
    expect(target.textContent).toContain("Change AGENTS.md");
    expect(target.querySelector("pre")?.textContent).toContain("+- Run ruff first.");
    expect(buttons()).toEqual(["Accept", "Edit", "Reject"]);
    expect(connection.sent).toEqual([]);
  });

  it("Accept sends steering_accept; the card leaves on the decision", () => {
    emit(PROPOSAL);
    click("Accept");
    expect(connection.sent).toEqual([
      { type: "steering_accept", session_id: "s1", proposal_id: "sp-1" },
    ]);
    expect(target.querySelector("article")).not.toBeNull();
    emit({ ...(fixtures.steering_decision as SteeringDecision), session_id: "s1", proposal_id: "sp-1" });
    expect(target.querySelector("article")).toBeNull();
  });

  it("Edit opens the text; Save sends what was typed", () => {
    emit(PROPOSAL);
    click("Edit");
    expect(buttons()).toEqual(["Save", "Cancel"]);
    const editor = target.querySelector("textarea")!;
    expect(editor.value).toBe(PROPOSAL.files[0]?.after);
    expect(connection.sent).toEqual([]);
    editor.value = "# Mine\n";
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    flushSync();
    click("Save");
    expect(connection.sent).toEqual([
      { type: "steering_edit", session_id: "s1", proposal_id: "sp-1", content: "# Mine\n" },
    ]);
  });

  it("Cancel goes back to the diff without sending", () => {
    emit(PROPOSAL);
    click("Edit");
    click("Cancel");
    expect(buttons()).toEqual(["Accept", "Edit", "Reject"]);
    expect(connection.sent).toEqual([]);
  });

  it("Reject sends steering_reject", () => {
    emit(PROPOSAL);
    click("Reject");
    expect(connection.sent).toEqual([
      { type: "steering_reject", session_id: "s1", proposal_id: "sp-1" },
    ]);
  });

  it("a card the daemon no longer holds says nothing was written and offers only Dismiss", () => {
    emit(PROPOSAL);
    click("Accept");
    emit({
      type: "error",
      seq: 1,
      session_id: "s1",
      code: "no_steering_proposal",
      message: "That steering proposal is no longer open, and nothing was written.",
      in_reply_to: "steering_accept",
    });
    expect(target.textContent).toContain("nothing was written");
    expect(buttons()).toEqual(["Dismiss"]);
    click("Dismiss");
    expect(target.querySelector("article")).toBeNull();
    expect(connection.sent).toHaveLength(1);
  });

  it("a fresh diff says the file changed, and a run's proposal says where it came from", () => {
    emit({ ...(fixtures.steering_proposal_fresh as SteeringProposal), session_id: "s1" });
    expect(target.textContent).toContain("The file changed since this was proposed");
    expect(target.textContent).toContain("From an unattended run");
  });
});
