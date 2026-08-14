// Presentation-mapping tests (AC #3, #6).

import { describe, it, expect } from "vitest";
import { KIND_LABELS, entryTone, classifyDiffLine, diffLines } from "./entry-view";
import type { TimelineEntry } from "./timeline";

function entry(kind: TimelineEntry["kind"], details: Record<string, unknown> = {}): TimelineEntry {
  return { id: `${kind}:1`, kind, seq: 1, title: "t", preview: "p", details };
}

describe("KIND_LABELS", () => {
  it("has a label for every entry kind", () => {
    const kinds: TimelineEntry["kind"][] = [
      "tool_call",
      "tool_result",
      "decision",
      "tier_switch",
      "compaction",
      "steering_reload",
      "error"
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

  it("marks compaction as warning and decisions/tier/steering as info", () => {
    expect(entryTone(entry("compaction"))).toBe("warning");
    expect(entryTone(entry("decision"))).toBe("info");
    expect(entryTone(entry("tier_switch"))).toBe("info");
    expect(entryTone(entry("steering_reload"))).toBe("info");
  });

  it("marks tool_call as neutral", () => {
    expect(entryTone(entry("tool_call"))).toBe("neutral");
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
