// Window math tests (AC #7).

import { describe, it, expect } from "vitest";
import { computeWindow, OVERSCAN_ROWS } from "./virtualization";

const ROW = 32;

describe("computeWindow", () => {
  it("returns an empty window for an empty list", () => {
    expect(computeWindow(0, ROW, 0, 640)).toEqual({ start: 0, end: 0, topPad: 0, bottomPad: 0 });
  });

  it("returns an empty window for a zero viewport", () => {
    expect(computeWindow(1000, ROW, 0, 0)).toEqual({ start: 0, end: 0, topPad: 0, bottomPad: 0 });
  });

  it("renders only the visible slice plus overscan at the top", () => {
    // 1000 rows, viewport shows 20 (640/32), at scrollTop 0.
    const w = computeWindow(1000, ROW, 0, 640);
    expect(w.start).toBe(0);
    expect(w.end).toBeLessThan(40); // 20 visible + 2*5 overscan, clamped to list
    expect(w.topPad).toBe(0);
    expect(w.bottomPad).toBe((1000 - w.end) * ROW);
  });

  it("clamps the top so it never renders a negative index", () => {
    const w = computeWindow(1000, ROW, 32 * 3, 640);
    expect(w.start).toBe(0); // 3 visible - 5 overscan clamps to 0
  });

  it("clamps the end so it never renders past the last entry", () => {
    const w = computeWindow(10, ROW, 32 * 100, 640);
    expect(w.end).toBe(10);
    expect(w.bottomPad).toBe(0);
  });

  it("renders fewer rows than the full list for a large session", () => {
    const w = computeWindow(1000, ROW, 32 * 500, 640);
    const rendered = w.end - w.start;
    expect(rendered).toBeLessThan(60);
    expect(rendered).toBeGreaterThan(0);
    // Padding preserves the total scroll height.
    expect(w.topPad + rendered * ROW + w.bottomPad).toBe(1000 * ROW);
  });

  it("uses a custom overscan when provided", () => {
    const a = computeWindow(1000, ROW, 0, 640, 0);
    const b = computeWindow(1000, ROW, 0, 640, OVERSCAN_ROWS);
    expect(a.end).toBeLessThan(b.end);
  });
});
