// Presentation-mapping tests (AC #3, #6).

import { describe, it, expect } from "vitest";
import { ICONS } from "./icons";
import {
  KIND_LABELS,
  entryTone,
  entryIcon,
  isScalarDetail,
  classifyDiffLine,
  diffLines,
  turnMetrics
} from "./entry-view";
import type { TimelineEntry } from "./timeline";

function entry(kind: TimelineEntry["kind"], details: Record<string, unknown> = {}): TimelineEntry {
  return { id: `${kind}:1`, kind, seq: 1, title: "t", preview: "p", details };
}

describe("KIND_LABELS", () => {
  it("has a label for every entry kind", () => {
    const kinds: TimelineEntry["kind"][] = [
      "turn",
      "tool_call",
      "tool_result",
      "decision",
      "approval",
      "tier_switch",
      "compaction",
      "steering_reload",
      "error",
      "verify"
    ];
    for (const k of kinds) expect(KIND_LABELS[k]).toBeTruthy();
  });
});

describe("entryTone", () => {
  it("marks errors and failed results as danger", () => {
    expect(entryTone(entry("error"))).toBe("danger");
    expect(entryTone(entry("tool_result", { status: "error" }))).toBe("danger");
  });

  it("marks successful results as success", () => {
    expect(entryTone(entry("tool_result", { status: "success" }))).toBe("success");
  });

  it("marks a denied result as warning, not danger", () => {
    expect(entryTone(entry("tool_result", { status: "error", error_code: "approval_denied" }))).toBe("warning");
    expect(entryTone(entry("tool_result", { status: "error" }))).toBe("danger");
  });

  it("marks an approval entry as warning", () => {
    expect(entryTone(entry("approval"))).toBe("warning");
  });

  it("marks compaction as warning and decisions/tier/steering as info", () => {
    expect(entryTone(entry("compaction"))).toBe("warning");
    expect(entryTone(entry("verify", { verdict: "pass" }))).toBe("success");
    expect(entryTone(entry("verify", { verdict: "fail" }))).toBe("danger");
    expect(entryTone(entry("verify", { pending: true }))).toBe("warning");
    expect(entryTone(entry("decision"))).toBe("info");
    expect(entryTone(entry("tier_switch"))).toBe("info");
    expect(entryTone(entry("steering_reload"))).toBe("info");
  });

  it("marks tool_call as neutral", () => {
    expect(entryTone(entry("tool_call"))).toBe("neutral");
  });

  it("colours a turn header by its outcome", () => {
    expect(entryTone(entry("turn", { status: "running" }))).toBe("neutral");
    expect(entryTone(entry("turn", { status: "complete" }))).toBe("neutral");
    expect(entryTone(entry("turn", { status: "cancelled" }))).toBe("warning");
    expect(entryTone(entry("turn", { status: "interrupted" }))).toBe("warning");
    expect(entryTone(entry("turn", { status: "failed" }))).toBe("danger");
  });
});

describe("turnMetrics", () => {
  it("says running until the daemon closes the turn", () => {
    expect(turnMetrics(entry("turn", { status: "running", duration: null, cost: null }))).toBe("running…");
  });

  it("reads duration and cost in the shared wording", () => {
    expect(turnMetrics(entry("turn", { status: "complete", duration: 12.4, cost: 0.03 }))).toBe("12s · $0.0300");
    expect(turnMetrics(entry("turn", { status: "complete", duration: 75, cost: 1.5 }))).toBe("1m 15s · $1.50");
  });

  it("appends failed, and names a stop by its state", () => {
    expect(turnMetrics(entry("turn", { status: "failed", duration: 3, cost: 0 }))).toBe("3s · $0.00 · failed");
    expect(turnMetrics(entry("turn", { status: "cancelled" }))).toBe("cancelled");
    expect(turnMetrics(entry("turn", { status: "interrupted" }))).toBe("interrupted");
  });
});

describe("entryIcon", () => {
  it("names an icon the shared map actually has, for every kind", () => {
    const kinds: TimelineEntry["kind"][] = [
      "turn",
      "tool_call",
      "tool_result",
      "decision",
      "approval",
      "tier_switch",
      "compaction",
      "steering_reload",
      "error"
    ];
    for (const k of kinds) expect(ICONS).toHaveProperty(entryIcon(entry(k)));
  });

  it("splits results on outcome the way the tone does", () => {
    expect(entryIcon(entry("tool_result", { status: "success" }))).toBe("check");
    expect(entryIcon(entry("tool_result", { status: "error" }))).toBe("x");
    expect(entryIcon(entry("tool_result", { status: "error", error_code: "approval_denied" }))).toBe("minus");
  });
});

describe("isScalarDetail", () => {
  it("reads strings, numbers, booleans and null inline", () => {
    for (const v of ["x", 3, true, null]) expect(isScalarDetail(v)).toBe(true);
  });

  it("sends objects and arrays to a code block", () => {
    expect(isScalarDetail({ a: 1 })).toBe(false);
    expect(isScalarDetail([1])).toBe(false);
  });
});

describe("classifyDiffLine", () => {
  it("classifies each unified-diff line kind", () => {
    expect(classifyDiffLine("--- a/a.txt")).toBe("meta");
    expect(classifyDiffLine("+++ b/a.txt")).toBe("meta");
    expect(classifyDiffLine("@@ -1 +1 @@")).toBe("hunk");
    expect(classifyDiffLine("+new line")).toBe("add");
    expect(classifyDiffLine("-old line")).toBe("del");
    expect(classifyDiffLine(" unchanged")).toBe("context");
  });
});

describe("diffLines", () => {
  it("splits a diff and drops a single trailing empty line", () => {
    expect(diffLines("-a\n+b\n")).toEqual(["-a", "+b"]);
  });

  it("handles a diff without a trailing newline", () => {
    expect(diffLines("-a\n+b")).toEqual(["-a", "+b"]);
  });
});
