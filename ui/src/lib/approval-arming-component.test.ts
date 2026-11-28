// @vitest-environment jsdom
//
// INTERACT-02 + APPROVE-01: the approval card, mounted for real behind the
// real bar. Two gated calls in a row; the second card mounts where the first
// was. The tail of the first answer (a double-click's second click, a second
// Enter, a held Enter) must not approve the second, and a card that appears
// while the person is typing must not take the keyboard.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const connection = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return { handlers, sent: [] as Array<Record<string, unknown>> };
});

vi.mock("./connection-status.svelte.js", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  onDaemonEvent: (handler: (event: unknown) => void) => {
    connection.handlers.add(handler);
    return () => connection.handlers.delete(handler);
  },
  sendToDaemon: (msg: Record<string, unknown>) => {
    connection.sent.push(msg);
    return true;
  },
}));

import ApprovalBar from "./components/ApprovalBar.svelte";
import { ARM_MS, cardKey, clickDecides, newArming, notePress } from "./approval-arming";
import { bindApprovals, pending } from "./approval-store.svelte.js";
import type { ApprovalRequest } from "./protocol";

function request(id: string, command: string): ApprovalRequest {
  return {
    type: "approval_request",
    session_id: "s1",
    tool_call_id: id,
    tool_name: "shell",
    arguments: { command },
    decision_class: "B",
    summary: `Run \`${command}\``,
    reason: "decision class B requires approval",
    seq: 1,
  };
}

function emit(event: unknown): void {
  for (const handler of connection.handlers) handler(event);
  flushSync();
}

let target: HTMLElement;
let app: ReturnType<typeof mount>;

beforeEach(() => {
  vi.useFakeTimers();
  bindApprovals(null);
  pending.splice(0, pending.length);
  connection.sent.length = 0;
  bindApprovals("s1");
  target = document.createElement("div");
  document.body.appendChild(target);
  app = mount(ApprovalBar, { target });
});

afterEach(() => {
  unmount(app);
  target.remove();
  vi.useRealTimers();
});

const decisions = () => connection.sent.filter((m) => m.type === "approve" || m.type === "deny");
const approveButton = () => target.querySelector<HTMLButtonElement>(".btn--approve")!;
const card = () => target.querySelector<HTMLElement>("[data-approval-card]")!;

function realClick(button: HTMLElement, detail = 1): void {
  button.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  button.dispatchEvent(new MouseEvent("click", { bubbles: true, detail }));
  flushSync();
}

function key(el: HTMLElement, init: KeyboardEventInit): void {
  el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }));
  flushSync();
}

function arm(): void {
  vi.advanceTimersByTime(ARM_MS);
  flushSync();
}

describe("a new card", () => {
  it("does not take focus from the composer", () => {
    const composer = document.createElement("textarea");
    document.body.appendChild(composer);
    composer.focus();
    emit(request("tc-1", "echo focus-steal-test"));
    arm();
    expect(document.activeElement).toBe(composer);
    expect(target.querySelector('[role="status"]')?.textContent).toContain("Approval needed");
    composer.remove();
  });

  it("is inert until it arms, and visibly so", () => {
    emit(request("tc-1", "rm -rf build"));
    expect(approveButton().getAttribute("aria-disabled")).toBe("true");
    realClick(approveButton());
    expect(decisions()).toEqual([]);
    arm();
    expect(approveButton().getAttribute("aria-disabled")).toBe("false");
    realClick(approveButton());
    expect(decisions()).toHaveLength(1);
  });

  it("plain Enter never approves; Ctrl+Enter does once armed", () => {
    emit(request("tc-1", "rm -rf build"));
    arm();
    key(card(), { key: "Enter" });
    expect(decisions()).toEqual([]);
    key(card(), { key: "Enter", ctrlKey: true });
    expect(decisions().map((m) => m.tool_call_id)).toEqual(["tc-1"]);
  });
});

describe("the tail of one answer does not answer the next card", () => {
  async function answerFirst(): Promise<void> {
    emit(request("tc-1", "rm -rf build"));
    emit(request("tc-2", "git push --force"));
    arm();
  }

  it("a double-click's second click lands on an unarmed card", async () => {
    emit(request("tc-1", "rm -rf build"));
    arm();
    realClick(approveButton(), 1);
    // The daemon asks for the next call at once, in the same spot.
    emit({ type: "tool_result", session_id: "s1", tool_call_id: "tc-1", status: "success", output: "", truncated: false, error_code: null, seq: 2 });
    emit(request("tc-2", "git push --force"));
    vi.advanceTimersByTime(120);
    realClick(approveButton(), 2);
    expect(decisions().map((m) => m.tool_call_id)).toEqual(["tc-1"]);
  });

  it("a click whose press began before the card armed does not count", () => {
    emit(request("tc-2", "git push --force"));
    approveButton().dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    arm();
    approveButton().dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
    flushSync();
    expect(decisions()).toEqual([]);
  });

  it("a second or held Ctrl+Enter is swallowed", async () => {
    await answerFirst();
    key(card(), { key: "Enter", ctrlKey: true });
    // Auto-repeat while the key is held.
    key(card(), { key: "Enter", ctrlKey: true, repeat: true });
    key(card(), { key: "Enter", ctrlKey: true, repeat: true });
    expect(decisions()).toHaveLength(1);
  });

  it("Esc on an unarmed card does nothing", () => {
    emit(request("tc-1", "rm -rf build"));
    key(card(), { key: "Escape" });
    expect(decisions()).toEqual([]);
  });
});

describe("the rules", () => {
  it("a keyboard activation of a focused button needs no press, but needs arming", () => {
    expect(clickDecides(newArming(), 0)).toBe(false);
    const armed = { armed: true, pressed: false };
    expect(clickDecides(armed, 0)).toBe(true);
    expect(clickDecides(armed, 1)).toBe(false);
    expect(clickDecides(notePress(armed), 1)).toBe(true);
    expect(notePress(newArming()).pressed).toBe(false);
  });

  it("chords", () => {
    const armed = { armed: true, pressed: false };
    const base = { repeat: false, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false };
    expect(cardKey(armed, { ...base, key: "Enter" })).toBeNull();
    expect(cardKey(armed, { ...base, key: "Enter", metaKey: true })).toBe("approve");
    expect(cardKey(armed, { ...base, key: "Enter", ctrlKey: true, shiftKey: true })).toBeNull();
    expect(cardKey(armed, { ...base, key: "a", repeat: true })).toBe("swallow");
    expect(cardKey(newArming(), { ...base, key: "Escape" })).toBe("swallow");
  });
});
