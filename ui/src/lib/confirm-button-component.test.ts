// @vitest-environment jsdom
//
// INTERACT-12 + SETMODEL-05: a double-click on a one-click Remove deleted two
// stored keys (the next row slid under the pointer), and a key went with no
// question asked and no way back. Now each asks, naming what goes and what
// uses it, the confirming button arms after ARM_MS on a fresh press, and once
// one fires every row action ignores input for ROW_QUIET_MS.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import ConfirmButton from "./components/ConfirmButton.svelte";
import { ARM_MS } from "./approval-arming";
import { ROW_QUIET_MS, resetRowGuard, rowActionFired, rowsQuiet } from "./row-guard";

let target: HTMLElement;
let apps: ReturnType<typeof mount>[] = [];
let removed: string[] = [];

function row(name: string): void {
  const el = document.createElement("div");
  el.dataset.row = name;
  target.appendChild(el);
  apps.push(
    mount(ConfirmButton, {
      target: el,
      props: {
        label: "Remove",
        question: `Remove the key ${name}? Used for: brain. The stored key cannot be restored.`,
        ariaLabel: `Remove the key ${name}`,
        onconfirm: () => removed.push(name),
      },
    }),
  );
}

function within(name: string): HTMLElement {
  return target.querySelector(`[data-row="${name}"]`)!;
}

function press(el: Element, detail = 1): void {
  el.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, detail }));
  flushSync();
}

beforeEach(() => {
  vi.useFakeTimers();
  resetRowGuard();
  removed = [];
  target = document.createElement("div");
  document.body.appendChild(target);
  for (const name of ["Alpha", "Bravo", "Charlie"]) row(name);
  flushSync();
});

afterEach(() => {
  for (const app of apps) unmount(app);
  apps = [];
  target.remove();
  vi.useRealTimers();
});

describe("a destructive row action", () => {
  it("asks first, naming the key and what uses it", () => {
    press(within("Alpha").querySelector("button")!);
    expect(removed).toEqual([]);
    const group = within("Alpha").querySelector('[role="group"]')!;
    expect(group.textContent).toContain("Remove the key Alpha? Used for: brain.");
    expect(group.textContent).toContain("cannot be restored");
  });

  it("a double-click's second press does not confirm", () => {
    press(within("Alpha").querySelector("button")!, 1);
    vi.advanceTimersByTime(120);
    press(within("Alpha").querySelector('[role="group"] button')!, 2);
    expect(removed).toEqual([]);
  });

  it("confirms on a fresh press once armed, and removes only that row", () => {
    press(within("Alpha").querySelector("button")!);
    vi.advanceTimersByTime(ARM_MS);
    flushSync();
    press(within("Alpha").querySelector('[role="group"] button')!);
    expect(removed).toEqual(["Alpha"]);
  });

  it("after one fires, the next row ignores input for a moment", () => {
    press(within("Alpha").querySelector("button")!);
    vi.advanceTimersByTime(ARM_MS);
    flushSync();
    press(within("Alpha").querySelector('[role="group"] button')!);
    // The row below arrives under the pointer: its Remove does not even ask.
    press(within("Bravo").querySelector("button")!);
    expect(within("Bravo").querySelector('[role="group"]')).toBeNull();
    vi.advanceTimersByTime(ROW_QUIET_MS);
    press(within("Bravo").querySelector("button")!);
    expect(within("Bravo").querySelector('[role="group"]')).not.toBeNull();
    expect(removed).toEqual(["Alpha"]);
  });

  it("Cancel and Escape put the button back", () => {
    press(within("Charlie").querySelector("button")!);
    const cancel = within("Charlie").querySelectorAll('[role="group"] button')[1];
    press(cancel);
    expect(within("Charlie").querySelector('[role="group"]')).toBeNull();
    press(within("Charlie").querySelector("button")!);
    within("Charlie")
      .querySelector('[role="group"]')!
      .dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    flushSync();
    expect(within("Charlie").querySelector('[role="group"]')).toBeNull();
    expect(removed).toEqual([]);
  });

  it("the guard's clock", () => {
    rowActionFired(1000);
    expect(rowsQuiet(1000 + ROW_QUIET_MS - 1)).toBe(true);
    expect(rowsQuiet(1000 + ROW_QUIET_MS)).toBe(false);
  });
});
