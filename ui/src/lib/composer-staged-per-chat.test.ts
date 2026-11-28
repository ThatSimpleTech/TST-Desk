// @vitest-environment jsdom
//
// COMPOSER-02: files staged in one chat are not in the composer of another.
// The composer stays mounted when the chat changes; the chips used to follow,
// and Enter sent chat A's files to chat B, which may run on a hosted model.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";

import { DEFAULT_ATTACHMENT_LIMITS } from "./attachments";
import Composer from "./components/chat/Composer.svelte";
import { resetSession, session } from "./session-status.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function stagedText(): string {
  return document.body.textContent ?? "";
}

async function stage(name: string): Promise<void> {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(["hello"], name, { type: "text/plain" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new Event("change", { bubbles: true }));
  for (let i = 0; i < 5; i++) await tick();
}

beforeEach(() => {
  resetSession();
  session.sessionId = "chat-a";
  app = mount(Composer, {
    target: document.body,
    props: {
      limits: { ...DEFAULT_ATTACHMENT_LIMITS },
      value: "",
      onsubmit: () => {},
    },
  });
});

afterEach(async () => {
  if (app !== null) await unmount(app);
  app = null;
  document.body.replaceChildren();
  resetSession();
});

describe("staged files", () => {
  it("stay with the chat they were staged in", async () => {
    await stage("private-notes.txt");
    expect(stagedText()).toContain("private-notes.txt");

    session.sessionId = "chat-b";
    flushSync();
    await tick();
    expect(stagedText()).not.toContain("private-notes.txt");

    session.sessionId = "chat-a";
    flushSync();
    await tick();
    expect(stagedText()).toContain("private-notes.txt");
  });
});
