// COMPOSER-08: Enter while an IME is composing accepts the candidate, and
// must not send. INTERACT-03: AltGr on Windows arrives as Ctrl+Alt, and on
// German, French and Spanish layouts it types the characters on the digit
// row; the model quick-switch (Ctrl+Alt+1..9) used to swallow them.

import { describe, expect, it } from "vitest";
import { composing } from "./ime";
import { modelSlot, resolveShortcut, type ShortcutEvent } from "./shortcuts";

const CONTEXT = { workspaceMenuOpen: false, paletteOpen: false, modalOpen: false, turnLive: false };

function winAltGr(key: string, digit: number, altGraph = true): ShortcutEvent {
  return { key, code: `Digit${digit}`, ctrlKey: true, altKey: true, metaKey: false, altGraph };
}

describe("composing", () => {
  it.each([
    [{ isComposing: true, keyCode: 13 }, false, true],
    [{ isComposing: false, keyCode: 229 }, false, true],
    [{ isComposing: false, keyCode: 13 }, true, true],
    [{ isComposing: false, keyCode: 13 }, false, false],
    [{}, false, false],
  ])("%o tracked=%s -> %s", (event, tracked, expected) => {
    expect(composing(event, tracked)).toBe(expected);
  });
});

describe("AltGr is never the model quick-switch", () => {
  it.each([
    ["de", "{", 7],
    ["de", "[", 8],
    ["de", "]", 9],
    ["fr", "~", 2],
    ["fr", "#", 3],
    ["fr", "{", 4],
    ["fr", "[", 5],
    ["fr", "|", 6],
    ["fr", "`", 7],
    ["fr", "\\", 8],
    ["fr", "^", 9],
    ["es", "|", 1],
    ["es", "@", 2],
    ["es", "#", 3],
    ["es", "~", 4],
  ])("%s AltGr+%s", (_layout, key, digit) => {
    // With AltGraph reported, and even where an engine does not report it.
    for (const reported of [true, false]) {
      const event = winAltGr(key, digit, reported);
      expect(modelSlot(event)).toBeNull();
      expect(resolveShortcut(event, CONTEXT)).toBeNull();
    }
  });

  it("Ctrl+Alt+digit that types the digit still switches", () => {
    const event = winAltGr("3", 3, false);
    expect(modelSlot(event)).toBe(3);
    expect(resolveShortcut(event, CONTEXT)).toBe("pick-model");
  });

  it("an AltGr press that types the digit is still not a shortcut", () => {
    expect(modelSlot(winAltGr("3", 3, true))).toBeNull();
  });

  it("⌘⌥1 on a Mac, where Option changes the character, still switches", () => {
    const mac: ShortcutEvent = { key: "¡", code: "Digit1", metaKey: true, ctrlKey: false, altKey: true };
    expect(modelSlot(mac)).toBe(1);
  });
});
