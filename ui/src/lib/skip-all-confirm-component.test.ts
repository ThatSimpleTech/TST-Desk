// @vitest-environment jsdom
//
// APPROVE-05: "Dangerously skip permissions" turned on with one click, with
// no confirmation, and nothing in the window showed it was on. Turning it on
// now asks, in plain words, with a confirm that arms after ARM_MS; turning it
// off is one click; and while it is on the title bar says so.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion } from "./protocol";

const mocks = vi.hoisted(() => ({ sent: [] as ClientMessageUnion[] }));

vi.mock("./connection-status.svelte.js", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  onEvent: () => () => undefined,
  sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
}));

import PolicyRuleList from "./components/PolicyRuleList.svelte";
import { ARM_MS } from "./approval-arming";
import { settings } from "./settings.svelte.js";

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

function skipSwitch(): HTMLButtonElement {
  return [...target.querySelectorAll<HTMLButtonElement>('button[role="switch"]')][0];
}

function press(el: Element): void {
  el.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
  flushSync();
}

beforeEach(() => {
  vi.useFakeTimers();
  mocks.sent = [];
  settings.skipAllApprovals = false;
  settings.rules = [];
  target = document.createElement("div");
  document.body.appendChild(target);
  component = mount(PolicyRuleList, { target, props: { sessionId: "s1" } });
  flushSync();
});

afterEach(() => {
  if (component !== null) unmount(component);
  component = null;
  target.remove();
  settings.skipAllApprovals = false;
  vi.useRealTimers();
});

describe("turning approvals off", () => {
  it("asks first, in plain words, and sends nothing until confirmed", () => {
    press(skipSwitch());
    const ask = target.querySelector('[role="alertdialog"]');
    expect(ask?.textContent).toContain("Turn off approvals for every session on this computer?");
    expect(ask?.textContent).toContain("including Class C");
    expect(mocks.sent).toEqual([]);
  });

  it("a press before the confirm arms does nothing; one after turns it on", () => {
    press(skipSwitch());
    const confirm = target.querySelector<HTMLButtonElement>('[role="alertdialog"] button')!;
    press(confirm);
    expect(mocks.sent).toEqual([]);
    vi.advanceTimersByTime(ARM_MS);
    flushSync();
    press(target.querySelector('[role="alertdialog"] button')!);
    expect(mocks.sent).toEqual([{ type: "set_skip_all_approvals", enabled: true }]);
  });

  it("Keep asking backs out", () => {
    press(skipSwitch());
    press(target.querySelectorAll('[role="alertdialog"] button')[1]);
    expect(target.querySelector('[role="alertdialog"]')).toBeNull();
    expect(mocks.sent).toEqual([]);
  });

  it("turning approvals back on is one click", () => {
    settings.skipAllApprovals = true;
    flushSync();
    press(skipSwitch());
    expect(mocks.sent).toEqual([{ type: "set_skip_all_approvals", enabled: false }]);
  });
});
