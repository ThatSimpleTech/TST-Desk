import { describe, it, expect } from "vitest";
import {
  DEFAULT_RAIL_PX,
  MAX_RAIL_PX,
  MIN_RAIL_PX,
  RAIL_STORAGE_KEY,
  clampRailPx,
  readPersistedRailPx,
  writePersistedRailPx,
} from "./rail-width.js";
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

describe("clampRailPx", () => {
  it("keeps in-range widths", () => {
    expect(clampRailPx(300)).toBe(300);
  });

  it("clamps to the bounds", () => {
    expect(clampRailPx(10)).toBe(MIN_RAIL_PX);
    expect(clampRailPx(9999)).toBe(MAX_RAIL_PX);
  });

  it("normalizes non-finite values to the default", () => {
    // The finite-guard fires before any bound math, matching clampLeftPct.
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

  it("writes clamped values only", () => {
    const storage = fakeStorage();
    writePersistedRailPx(storage, 1);
    expect(storage.store.get(RAIL_STORAGE_KEY)).toBe(String(MIN_RAIL_PX));
  });
});
