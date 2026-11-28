// @vitest-environment jsdom
//
// COMPOSER-08, mounted: the composer does not send on an Enter that belongs
// to an input method, including WebKit's, which arrives just after
// compositionend with no composing flag.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const sent = vi.hoisted(() => [] as unknown[]);

vi.mock("./connection-status.svelte.js", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  sendToDaemon: (msg: unknown) => {
    sent.push(msg);
    return true;
  },
}));

import Composer from "./components/chat/Composer.svelte";
import { DEFAULT_ATTACHMENT_LIMITS } from "./attachments";

let target: HTMLElement;
let app: ReturnType<typeof mount>;

beforeEach(() => {
  sent.length = 0;
  target = document.createElement("div");
  document.body.appendChild(target);
  app = mount(Composer, { target, props: {
      limits: DEFAULT_ATTACHMENT_LIMITS,
      onsubmit: (...args: unknown[]) => void sent.push(args),
    } });
  flushSync();
});

afterEach(() => {
  unmount(app);
  target.remove();
});

function textarea(): HTMLTextAreaElement {
  return target.querySelector("textarea")!;
}

function type(text: string): void {
  textarea().value = text;
  textarea().dispatchEvent(new Event("input", { bubbles: true }));
  flushSync();
}

function enter(init: KeyboardEventInit & { keyCode?: number } = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, ...init });
  if (init.keyCode !== undefined) Object.defineProperty(event, "keyCode", { value: init.keyCode });
  textarea().dispatchEvent(event);
  flushSync();
  return event;
}

describe("Enter during IME composition", () => {
  it("does not send while composing, by either signal", () => {
    type("にほん");
    expect(enter({ isComposing: true }).defaultPrevented).toBe(false);
    expect(enter({ keyCode: 229 }).defaultPrevented).toBe(false);
    expect(sent).toEqual([]);
  });

  it("does not send on WebKit's Enter right after compositionend", async () => {
    type("你好");
    textarea().dispatchEvent(new CompositionEvent("compositionstart"));
    textarea().dispatchEvent(new CompositionEvent("compositionend"));
    expect(enter().defaultPrevented).toBe(false);
    expect(sent).toEqual([]);
    await new Promise((r) => setTimeout(r, 0));
    // The next Enter, once the composition is over, is the person's own.
    expect(enter().defaultPrevented).toBe(true);
    expect(sent).toHaveLength(1);
  });
});
