// Working-state flavor tests (TD-1713): the verb rotation's cadence and
// wrap, pinned so a refactor can't quietly change the shop's rhythm.

import { describe, it, expect } from "vitest";
import { VERB_ROTATE_MS, WORKING_VERBS, workingVerb } from "./working-flavor";

describe("workingVerb", () => {
  it("starts on the first verb from 0 up to just before the cadence", () => {
    expect(workingVerb(0)).toBe(WORKING_VERBS[0]);
    expect(workingVerb(VERB_ROTATE_MS - 1)).toBe(WORKING_VERBS[0]);
  });

  it("rotates exactly on the cadence boundary", () => {
    expect(workingVerb(VERB_ROTATE_MS)).toBe(WORKING_VERBS[1]);
    expect(workingVerb(VERB_ROTATE_MS * 2)).toBe(WORKING_VERBS[2]);
  });

  it("wraps the list instead of running off the end", () => {
    expect(workingVerb(VERB_ROTATE_MS * WORKING_VERBS.length)).toBe(WORKING_VERBS[0]);
    expect(workingVerb(VERB_ROTATE_MS * (WORKING_VERBS.length + 1))).toBe(WORKING_VERBS[1]);
  });

  it("clamps a negative elapsed to the first verb", () => {
    expect(workingVerb(-50)).toBe(WORKING_VERBS[0]);
  });

  it("keeps the stable in voice: unique one-word participles, no emoji", () => {
    expect(new Set(WORKING_VERBS).size).toBe(WORKING_VERBS.length);
    for (const verb of WORKING_VERBS) {
      expect(verb).toMatch(/^[A-Z][a-z]+$/);
    }
  });
});
