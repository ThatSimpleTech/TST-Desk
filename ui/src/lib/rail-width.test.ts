import { describe, it, expect } from "vitest";
import {
  DEFAULT_RAIL_PX,
  MIN_RAIL_PX,
  RAIL_STORAGE_KEY,
  clampRailPx,
  railMaxPx,
  readPersistedRailPx,
  writePersistedRailPx,
} from "./rail-width.js";
import { MIN_LEFT_PX, MIN_RIGHT_PX } from "./splitpane.js";
import type { KVStorage } from "./splitpane.js";

function fakeStorage(initial: Record<string, string> = {}): KVStorage & {
  store: Map<string, string>;
} {
  const store = new Map(Object.entries(initial));
  return {
    store,
    getItem: (key) => (store.has(key) ? store.get(key)! : null),
    setItem: (key, value) => void store.set(key, value),
  };
}

describe("railMaxPx", () => {
  it("leaves the chat and inspector their split minima", () => {
    expect(railMaxPx(1400)).toBe(1400 - MIN_LEFT_PX - MIN_RIGHT_PX);
  });

  it("does not drop below the rail minimum on a narrow viewport", () => {
    expect(railMaxPx(400)).toBe(MIN_RAIL_PX);
    expect(railMaxPx(0)).toBe(MIN_RAIL_PX);
  });
});

describe("clampRailPx", () => {
  it("keeps in-range widths", () => {
    expect(clampRailPx(300)).toBe(300);
  });

  it("floors at the minimum and has no fixed ceiling", () => {
    expect(clampRailPx(10)).toBe(MIN_RAIL_PX);
    expect(clampRailPx(9999)).toBe(9999);
  });

  it("honours a layout max when one is supplied", () => {
    expect(clampRailPx(9999, 500)).toBe(500);
    expect(clampRailPx(300, 500)).toBe(300);
  });

  it("normalizes non-finite values to the default", () => {
    // The finite-guard fires before any bound math, matching clampRightPx.
    expect(clampRailPx(Number.NaN)).toBe(DEFAULT_RAIL_PX);
    expect(clampRailPx(Number.POSITIVE_INFINITY)).toBe(DEFAULT_RAIL_PX);
  });
});

describe("persistence", () => {
  it("round-trips a width", () => {
    const storage = fakeStorage();
    writePersistedRailPx(storage, 320);
    expect(readPersistedRailPx(storage)).toBe(320);
  });

  it("round-trips a width past the old 440px ceiling", () => {
    const storage = fakeStorage();
    writePersistedRailPx(storage, 720);
    expect(readPersistedRailPx(storage)).toBe(720);
  });

  it("falls back to the default when nothing is stored", () => {
    expect(readPersistedRailPx(fakeStorage())).toBe(DEFAULT_RAIL_PX);
  });

  it("falls back to the default on corrupt or blank values", () => {
    expect(readPersistedRailPx(fakeStorage({ [RAIL_STORAGE_KEY]: "wide" }))).toBe(
      DEFAULT_RAIL_PX
    );
    expect(readPersistedRailPx(fakeStorage({ [RAIL_STORAGE_KEY]: "  " }))).toBe(
      DEFAULT_RAIL_PX
    );
  });

  it("writes floored values only", () => {
    const storage = fakeStorage();
    writePersistedRailPx(storage, 1);
    expect(storage.store.get(RAIL_STORAGE_KEY)).toBe(String(MIN_RAIL_PX));
  });
});
