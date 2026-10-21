// @vitest-environment jsdom
//
// After-write hooks in the step timeline (TD-5135). A write whose
// formatter or linter ran shows the time the hooks took on the collapsed
// row, and each hook's outcome when the row is expanded.
import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import TimelineEntryRow from "./components/TimelineEntryRow.svelte";
import type { DaemonEventUnion, ToolResult } from "./protocol";
import fixtures from "./protocol-fixtures.json";
import { eventToEntry, formatHookRuns, hookSummary, Timeline } from "./timeline";

const afterWrite = fixtures.tool_result_after_write as ToolResult;

describe("after-write hooks on the step timeline", () => {
  it("the fixture is the daemon's own shape", () => {
    expect(afterWrite.hooks?.map((h) => h.status)).toEqual(["passed", "failed"]);
  });

  it("puts the hooks' total time on the collapsed row", () => {
    const entry = eventToEntry(afterWrite as DaemonEventUnion);
    expect(entry?.progress).toBe("after write failed · 0.43 s");
  });

  it("lists each hook with its time and outcome", () => {
    const entry = eventToEntry(afterWrite as DaemonEventUnion);
    expect(entry?.details.after_write).toBe(
      "ruff format · 0.12 s · ok, changed the file\nruff check · 0.31 s · failed (exit 1)",
    );
  });

  it("says nothing for a call that ran no hook", () => {
    const entry = eventToEntry(fixtures.tool_result as DaemonEventUnion);
    expect(entry?.progress).toBeUndefined();
    expect(entry?.details).not.toHaveProperty("after_write");
  });

  it("formats passing, slow, timed-out and unstartable hooks", () => {
    expect(hookSummary([{ name: "fmt", status: "passed", duration_ms: 50 }])).toBe("after write 0.05 s");
    expect(
      formatHookRuns([
        { name: "slow", status: "timed_out", duration_ms: 30_000 },
        { name: "gone", status: "error", duration_ms: 0 },
      ]),
    ).toBe("slow · 30 s · timed out\ngone · 0.00 s · could not start");
  });

  it("survives the Timeline's push path", () => {
    const t = new Timeline();
    t.bind(afterWrite.session_id);
    t.push(afterWrite as DaemonEventUnion);
    expect(t.entries.at(-1)?.progress).toBe("after write failed · 0.43 s");
  });

  it("shows on the row: the time collapsed, each hook expanded", () => {
    const entry = eventToEntry(afterWrite as DaemonEventUnion);
    if (entry === null) throw new Error("no entry");
    const target = document.createElement("div");
    document.body.appendChild(target);
    const row = mount(TimelineEntryRow, { target, props: { entry, expanded: true, ontoggle: () => {} } });
    flushSync();
    expect(target.querySelector(".progress")?.textContent).toBe("after write failed · 0.43 s");
    expect(target.textContent).toContain("ruff check · 0.31 s · failed (exit 1)");
    unmount(row);
    target.remove();
  });
});
