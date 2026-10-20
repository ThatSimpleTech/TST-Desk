// @vitest-environment jsdom
//
// TD-5123: the plan review card, mounted for real. Read-only plan mode ends
// with the agent's plan: Approve ends plan mode with it pinned, Edit lets
// the person change it first, and Reject keeps plan mode on. The phone shows
// this same card. Every button only sends the person's review.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const connection = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return { handlers, sent: [] as unknown[], up: true };
});

vi.mock("./connection-status.svelte.js", () => ({
  onDaemonEvent: (handler: (event: unknown) => void) => {
    connection.handlers.add(handler);
    return () => connection.handlers.delete(handler);
  },
  sendToDaemon: (msg: unknown) => {
    if (!connection.up) return false;
    connection.sent.push(msg);
    return true;
  },
}));

import PlanReviewBar from "./components/PlanReviewBar.svelte";
import fixtures from "./protocol-fixtures.json";
import { bindPlanReviews, pending } from "./plan-review-store.svelte.js";
import type { Error as DaemonError, PlanReview, PlanReviewed, ToolResult, TurnComplete } from "./protocol";

const REVIEW: PlanReview = { ...(fixtures.plan_review as PlanReview), session_id: "s1" };

type Event = PlanReview | PlanReviewed | ToolResult | TurnComplete | DaemonError;

function emit(event: Event): void {
  for (const handler of connection.handlers) handler(event);
  flushSync();
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

beforeEach(() => {
  connection.sent.length = 0;
  connection.up = true;
  bindPlanReviews(null);
  bindPlanReviews("s1");
  target = document.createElement("div");
  document.body.appendChild(target);
  component = mount(PlanReviewBar, { target });
  flushSync();
});

afterEach(() => {
  if (component !== null) unmount(component);
  component = null;
  target.remove();
});

function button(label: string): HTMLButtonElement {
  const found = [...target.querySelectorAll("button")].find(
    (b) => (b.textContent ?? "").replace(/\s+/g, " ").trim() === label,
  );
  expect(found, label).toBeDefined();
  return found!;
}

function click(label: string): void {
  button(label).click();
  flushSync();
}

function fill(selector: string, text: string): void {
  const field = target.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
  field.value = text;
  field.dispatchEvent(new Event("input", { bubbles: true }));
  flushSync();
}

describe("plan review card", () => {
  it("shows the plan rendered, for the bound session only", () => {
    emit({ ...REVIEW, session_id: "s2", tool_call_id: "other" });
    expect(target.querySelector("[data-plan-card]")).toBeNull();
    emit(REVIEW);
    const card = target.querySelector("[data-plan-card]")!;
    expect(card.getAttribute("aria-label")).toBe("The agent's plan");
    expect(card.querySelector("h2")?.textContent).toBe("Plan");
    expect(card.textContent).toContain("Add greet() to src/app.py.");
    expect(card.textContent).toContain("Plan mode: nothing has changed yet");
  });

  it("approves the plan as written, with the note", () => {
    emit(REVIEW);
    fill('input[aria-label="Note for the agent"]', "go ahead");
    click("Approve plan");
    expect(connection.sent).toEqual([
      { type: "review_plan", session_id: "s1", tool_call_id: "tc-p", decision: "approve", plan: null, feedback: "go ahead" },
    ]);
    // The card waits for the daemon, and a second click sends nothing.
    expect(target.textContent).toContain("Sending your review");
    click("Approve plan");
    expect(connection.sent).toHaveLength(1);
    emit({ type: "plan_reviewed", session_id: "s1", tool_call_id: "tc-p", outcome: "approved", plan: REVIEW.plan, edited: false, feedback: "go ahead", seq: 12 });
    expect(target.querySelector("[data-plan-card]")).toBeNull();
  });

  it("edits the plan, then approves the edit", () => {
    emit(REVIEW);
    click("Edit plan");
    const editor = target.querySelector<HTMLTextAreaElement>('textarea[aria-label="Edit the plan"]')!;
    expect(editor.value).toBe(REVIEW.plan);
    fill('textarea[aria-label="Edit the plan"]', "1. Only add greet().");
    click("Show plan");
    expect(target.querySelector("textarea")).toBeNull();
    expect(target.textContent).toContain("Only add greet().");
    click("Approve edited plan");
    expect(connection.sent).toEqual([
      { type: "review_plan", session_id: "s1", tool_call_id: "tc-p", decision: "approve", plan: "1. Only add greet().", feedback: "" },
    ]);
  });

  it("will not approve an empty plan", () => {
    emit(REVIEW);
    click("Edit plan");
    fill('textarea[aria-label="Edit the plan"]', "   ");
    expect(button("Approve edited plan").disabled).toBe(true);
  });

  it("rejects with the note", () => {
    emit(REVIEW);
    fill('input[aria-label="Note for the agent"]', "use the logger");
    click("Reject plan");
    expect(connection.sent).toEqual([
      { type: "review_plan", session_id: "s1", tool_call_id: "tc-p", decision: "reject", feedback: "use the logger" },
    ]);
  });

  it("shows a refusal on the card and lets the person try again", () => {
    emit(REVIEW);
    click("Approve plan");
    emit({ type: "error", code: "bad_plan", message: "An approved plan can't be empty.", session_id: "s1", in_reply_to: "review_plan", seq: 0 } as DaemonError);
    expect(target.querySelector('[role="alert"]')?.textContent).toBe("An approved plan can't be empty.");
    click("Approve plan");
    expect(connection.sent).toHaveLength(2);
  });

  it("drops the card when the daemon has nothing waiting, the call ends or the turn ends", () => {
    emit(REVIEW);
    click("Reject plan");
    emit({ type: "error", code: "no_pending_plan", message: "gone", session_id: "s1", in_reply_to: "review_plan", seq: 0 } as DaemonError);
    expect(pending).toHaveLength(0);
    emit(REVIEW);
    emit({ type: "tool_result", session_id: "s1", tool_call_id: "tc-p", status: "error", output: "stopped", seq: 13 } as ToolResult);
    expect(pending).toHaveLength(0);
    emit(REVIEW);
    emit({ type: "turn_complete", session_id: "s1", seq: 14 } as TurnComplete);
    expect(pending).toHaveLength(0);
  });

  it("says when it could not send", () => {
    emit(REVIEW);
    connection.up = false;
    click("Approve plan");
    expect(connection.sent).toEqual([]);
    expect(target.querySelector('[role="alert"]')?.textContent).toContain("Not sent");
  });

  it("keeps Esc from reaching the shell, which would cancel the turn", () => {
    emit(REVIEW);
    click("Edit plan");
    const card = target.querySelector<HTMLElement>("[data-plan-card]")!;
    const esc = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    // The shell's handler listens on the window, past the mount root.
    let reachedShell = false;
    const shell = (): void => {
      reachedShell = true;
    };
    window.addEventListener("keydown", shell);
    card.dispatchEvent(esc);
    window.removeEventListener("keydown", shell);
    flushSync();
    expect(esc.defaultPrevented).toBe(true);
    expect(reachedShell).toBe(false);
    expect(target.querySelector("textarea")).toBeNull();
  });
});
