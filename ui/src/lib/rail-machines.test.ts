// The rail with other computers (TD-5015): sections, names, keyboard order
// and the actions a remote row offers. With no paired computer every helper
// must hand back exactly what the single list used before.
import { describe, expect, it } from "vitest";
import { THIS_COMPUTER, machineLabel, railSections, sessionOrderAcross, stripRows } from "./machines";
import type { MachineSummary } from "./protocol";
import { groupRowsByRecency, railOrder, rowActions } from "./rail";

const MCH_A = "mch_aaaaaaaaaaaa";
const MCH_B = "mch_bbbbbbbbbbbb";
const MCH_GONE = "mch_cccccccccccc";

function machine(over: Partial<MachineSummary> = {}): MachineSummary {
  return {
    id: MCH_A,
    name: "EZER",
    host: "100.64.0.2",
    port: 39079,
    status: "connected",
    last_seen: null,
    paired_at: "2026-10-01T09:00:00Z",
    same_computer: false,
    detail: null,
    ...over,
  };
}

interface Row {
  sessionId: string;
  updatedAt: string;
  starred: boolean;
}

function row(sessionId: string, updatedAt: string, starred = false): Row {
  return { sessionId, updatedAt, starred };
}

const now = new Date();
const today = (h: number) => new Date(now.getFullYear(), now.getMonth(), now.getDate(), h).toISOString();
const daysAgo = (d: number) => new Date(now.getFullYear(), now.getMonth(), now.getDate() - d, 9).toISOString();

const ROWS: Row[] = [
  row("own-1", today(0)),
  row(`r1:${MCH_A}`, today(0)),
  row("own-2", daysAgo(3)),
  row(`r2:${MCH_B}`, daysAgo(1)),
  row(`r3:${MCH_A}`, daysAgo(10), true),
  row(`r4:${MCH_GONE}`, today(0)),
];

describe("railSections", () => {
  it("is one section holding every row when no computer is paired", () => {
    const sections = railSections(ROWS, []);
    expect(sections).toHaveLength(1);
    expect(sections[0]).toMatchObject({ id: "local", machine: null, title: THIS_COMPUTER });
    expect(sections[0].rows).toEqual(ROWS);
  });

  it("puts this computer first, then each paired computer in the hub's order", () => {
    const a = machine();
    const b = machine({ id: MCH_B, name: "mini" });
    const sections = railSections(ROWS, [b, a], "studio");
    expect(sections.map((s) => s.id)).toEqual(["local", MCH_B, MCH_A, MCH_GONE]);
    expect(sections[0].title).toBe("studio");
    expect(sections[0].rows.map((r) => r.sessionId)).toEqual(["own-1", "own-2"]);
    expect(sections[1].rows.map((r) => r.sessionId)).toEqual([`r2:${MCH_B}`]);
    expect(sections[2].rows.map((r) => r.sessionId)).toEqual([`r1:${MCH_A}`, `r3:${MCH_A}`]);
  });

  it("keeps a section for a computer with no rows, and never hides an unlisted computer's rows", () => {
    const sections = railSections([row("own-1", today(0)), row(`x:${MCH_GONE}`, today(0))], [machine()]);
    expect(sections.find((s) => s.id === MCH_A)?.rows).toEqual([]);
    const orphan = sections.find((s) => s.id === MCH_GONE);
    expect(orphan).toMatchObject({ machine: null, title: "Another computer" });
    expect(orphan?.rows.map((r) => r.sessionId)).toEqual([`x:${MCH_GONE}`]);
  });
});

describe("machineLabel", () => {
  it("is the plain name when it is unique", () => {
    expect(machineLabel(machine(), [machine()])).toBe("EZER");
  });

  it("marks a self-pair", () => {
    const self = machine({ same_computer: true, name: "studio" });
    expect(machineLabel(self, [self])).toBe("studio (this computer)");
  });

  it("tells same-named computers apart by host, then host:port, then handle", () => {
    const a = machine();
    const b = machine({ id: MCH_B, host: "100.64.0.9" });
    expect(machineLabel(a, [a, b])).toBe("EZER (100.64.0.2)");
    expect(machineLabel(b, [a, b])).toBe("EZER (100.64.0.9)");
    const c = machine({ id: MCH_B, port: 39080 });
    expect(machineLabel(a, [a, c])).toBe("EZER (100.64.0.2:39079)");
    expect(machineLabel(c, [a, c])).toBe("EZER (100.64.0.2:39080)");
    // A phone is never sent addresses.
    const p = machine({ host: null, port: null });
    const q = machine({ id: MCH_B, host: null, port: null });
    expect(machineLabel(p, [p, q])).toBe("EZER (aaaa)");
    expect(machineLabel(q, [p, q])).toBe("EZER (bbbb)");
  });
});

describe("keyboard and strip order", () => {
  it("matches the single list exactly with no paired computer", () => {
    expect(sessionOrderAcross(ROWS, [])).toEqual(railOrder(groupRowsByRecency(ROWS)));
    expect(stripRows(ROWS, [])).toEqual(ROWS);
  });

  it("walks each computer's block in turn, top to bottom as drawn", () => {
    const order = sessionOrderAcross(ROWS, [machine(), machine({ id: MCH_B, name: "mini" })]);
    expect(order).toEqual(["own-1", "own-2", `r3:${MCH_A}`, `r1:${MCH_A}`, `r2:${MCH_B}`, `r4:${MCH_GONE}`]);
    expect(new Set(order).size).toBe(ROWS.length);
  });

  it("orders the collapsed strip by section", () => {
    const strip = stripRows(ROWS, [machine({ id: MCH_B, name: "mini" })]);
    expect(strip.map((r) => r.sessionId)).toEqual([
      "own-1",
      "own-2",
      `r2:${MCH_B}`,
      `r1:${MCH_A}`,
      `r3:${MCH_A}`,
      `r4:${MCH_GONE}`,
    ]);
  });
});

describe("rowActions for a remote row", () => {
  it("drops Move and Schedule, and keeps the rest", () => {
    const local = rowActions(false, false).map((a) => a.id);
    const remote = rowActions(false, false, true).map((a) => a.id);
    expect(local).toContain("move");
    expect(local).toContain("schedule");
    expect(remote).not.toContain("move");
    expect(remote).not.toContain("schedule");
    expect(remote).toEqual(local.filter((id) => id !== "move" && id !== "schedule"));
  });

  it("is unchanged for this computer's rows", () => {
    expect(rowActions(true, true)).toEqual(rowActions(true, true, false));
  });
});
