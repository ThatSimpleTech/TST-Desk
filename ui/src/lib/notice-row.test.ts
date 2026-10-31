// @vitest-environment jsdom
//
// The line an older chat shows about messages it cannot show (TD-5201),
// mounted for real through MessageBubble, as the transcript renders it.

import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

vi.mock("./connection-status.svelte.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("./connection-status.svelte.js")>();
  return {
    ...real,
    onDaemonEvent: () => () => {},
    onConnectionState: () => () => {},
    onResume: () => () => {},
    sendToDaemon: () => true,
    attachToSession: () => {},
    detachFromSession: () => {},
  };
});

import type { ChatMessage } from "./chat-store";
import MessageBubble from "./components/chat/MessageBubble.svelte";

let mounted: Record<string, unknown> | null = null;

function render(message: ChatMessage): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted = mount(MessageBubble, { target, props: { message } });
  flushSync();
  return target;
}

function notice(overrides: Partial<ChatMessage>): ChatMessage {
  return { id: "m1", role: "notice", text: "", complete: true, at: 0, ...overrides };
}

afterEach(() => {
  if (mounted !== null) unmount(mounted);
  mounted = null;
  document.body.innerHTML = "";
});

describe("a notice row (TD-5201)", () => {
  it("says plainly that a trimmed chat's earlier messages can't be shown, and offers no actions", () => {
    const target = render(notice({ notice: "trimmed" }));
    const line = target.querySelector('[role="note"]');
    expect(line?.textContent?.trim()).toBe("Earlier messages in this chat can't be shown.");
    expect(target.querySelector("button")).toBeNull();
  });

  it("folds a summary's text under its line", () => {
    const target = render(notice({ notice: "summarized", foldedTurns: 4, text: "Planned the beds." }));
    expect(target.textContent).toContain("4 earlier turns were summarized to save room.");
    expect(target.textContent).not.toContain("Planned the beds.");
    const fold = target.querySelector("button");
    expect(fold?.textContent).toContain("Summary");
    fold?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    flushSync();
    expect(target.textContent).toContain("Planned the beds.");
  });
});
