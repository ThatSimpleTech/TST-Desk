// @vitest-environment jsdom
//
// JOURNEY2-10: on Scheduled, beside a job "Waiting for approval · Run `npm
// test`", the bar's Approve approved a force-push in another chat; the card
// named no chat. A card now says whose it is, and off the chat surface the
// bar is a named chip that opens the chat, with no Approve or Deny.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const connection = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return { handlers, sent: [] as unknown[] };
});

vi.mock("./connection-status.svelte.js", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  onDaemonEvent: (handler: (event: unknown) => void) => {
    connection.handlers.add(handler);
    return () => connection.handlers.delete(handler);
  },
  sendToDaemon: (msg: unknown) => {
    connection.sent.push(msg);
    return true;
  },
}));

import ApprovalBar from "./components/ApprovalBar.svelte";
import { chatLabel } from "./approval-context";
import { bindApprovals, pending } from "./approval-store.svelte.js";
import { projects } from "./projects.svelte.js";
import { sessions } from "./sessions.svelte.js";

function emit(event: unknown): void {
  for (const handler of connection.handlers) handler(event);
  flushSync();
}

let target: HTMLElement;
let app: ReturnType<typeof mount>;

beforeEach(() => {
  bindApprovals(null);
  pending.splice(0, pending.length);
  bindApprovals("s1");
  sessions.rows = [
    { sessionId: "s1", title: "trip notes", workspacePath: "/home/me/notes-repo" },
  ] as unknown as typeof sessions.rows;
  projects.surface = "home";
  target = document.createElement("div");
  document.body.appendChild(target);
  app = mount(ApprovalBar, { target });
  emit({
    type: "approval_request",
    session_id: "s1",
    tool_call_id: "tc-1",
    tool_name: "shell",
    arguments: { command: "git push --force origin main" },
    decision_class: "C",
    summary: "Run `git push --force origin main`",
    reason: "decision class C",
    seq: 1,
  });
});

afterEach(() => {
  unmount(app);
  target.remove();
  sessions.rows = [];
  projects.surface = "home";
});

describe("an approval says whose chat it is", () => {
  it("on the chat surface the card names the chat and folder", () => {
    const card = target.querySelector("[data-approval-card]")!;
    expect(card.textContent).toContain("trip notes · notes-repo");
    expect(card.getAttribute("aria-label")).toBe(
      "Approval required in trip notes · notes-repo: Run `git push --force origin main`",
    );
  });

  it("on Scheduled it is a chip that opens the chat, with no Approve or Deny", () => {
    projects.surface = "scheduled";
    flushSync();
    expect(target.querySelector("[data-approval-card]")).toBeNull();
    expect(target.querySelector(".btn--approve")).toBeNull();
    expect(target.textContent).toContain("trip notes · notes-repo is waiting for approval");
    (target.querySelector(".chip-review") as HTMLButtonElement).click();
    flushSync();
    expect(projects.surface).toBe("home");
    expect(target.querySelector("[data-approval-card]")).not.toBeNull();
    expect(connection.sent).toEqual([]);
  });

  it("an untitled or unknown chat still says which", () => {
    expect(chatLabel("abcdef1234", [])).toBe("Chat abcdef12");
    expect(
      chatLabel("s2", [{ sessionId: "s2", title: null, workspacePath: "C:\\work\\api\\" }]),
    ).toBe("Chat s2 · api");
  });
});
