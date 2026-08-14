// Tests for the doctor store (TD-1104 diagnostics).
//
// The store touches the daemon only through connection-status's
// sendToDaemon/onEvent, so the tests mock exactly that seam and drive the
// same diagnostics_report events the daemon emits.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, DiagnosticCheck } from "./protocol";

const mocks = vi.hoisted(() => {
  const state = {
    handler: null as ((e: DaemonEventUnion) => void) | null,
    sent: [] as ClientMessageUnion[],
    sendOk: true,
  };
  return state;
});

vi.mock("./connection-status.svelte.js", () => ({
  onEvent: (handler: (e: DaemonEventUnion) => void) => {
    mocks.handler = handler;
    return () => {
      mocks.handler = null;
    };
  },
  sendToDaemon: (msg: ClientMessageUnion) => {
    mocks.sent.push(msg);
    return mocks.sendOk;
  },
}));

import {
  doctor,
  startDoctor,
  resetDoctor,
  runDoctor,
  closeDoctor,
  buildDoctorReport,
  copyDoctorReport,
} from "./doctor.svelte.js";

function emit(event: DaemonEventUnion): void {
  mocks.handler?.(event);
}

const report = (checks: DiagnosticCheck[]): DaemonEventUnion =>
  ({ type: "diagnostics_report", seq: 1, checks }) as DaemonEventUnion;

beforeEach(() => {
  resetDoctor();
  mocks.sent.length = 0;
  mocks.sendOk = true;
});

describe("run/reduce lifecycle", () => {
  it("opens, marks running, and sends run_diagnostics", () => {
    startDoctor();
    runDoctor();
    expect(doctor.open).toBe(true);
    expect(doctor.running).toBe(true);
    expect(mocks.sent.some((m) => m.type === "run_diagnostics")).toBe(true);
  });

  it("the report settles running and exposes rows", () => {
    startDoctor();
    runDoctor();
    emit(report([{ name: "git", status: "ok", detail: "git 2.50.0" }]));
    expect(doctor.running).toBe(false);
    expect(doctor.checks).toHaveLength(1);
    expect(doctor.checks[0].name).toBe("git");
  });

  it("a re-run while in flight does not double-send", () => {
    startDoctor();
    runDoctor();
    runDoctor();
    expect(mocks.sent.filter((m) => m.type === "run_diagnostics")).toHaveLength(1);
  });

  it("socket down reports honestly instead of hanging", () => {
    startDoctor();
    mocks.sendOk = false;
    runDoctor();
    expect(doctor.running).toBe(false);
    expect(doctor.checks[0].status).toBe("fail");
    expect(doctor.checks[0].fix).toBeTruthy();
  });

  it("closing hides the pane but keeps the last report", () => {
    startDoctor();
    runDoctor();
    const rows = [{ name: "daemon", status: "ok", detail: "responding" } as DiagnosticCheck];
    emit(report(rows));
    closeDoctor();
    expect(doctor.open).toBe(false);
    expect(doctor.checks).toEqual(rows);
  });
});

describe("report copy", () => {
  it("renders marks per status and indents fixes under fails", () => {
    const text = buildDoctorReport([
      { name: "daemon", status: "ok", detail: "responding (v0.1.0)" },
      { name: "api_key", status: "fail", detail: "no API key stored", fix: "Open the wizard." },
      { name: "provider", status: "skip", detail: "not checked" },
    ]);
    const lines = text.split("\n");
    expect(lines[0]).toBe("TST Desk doctor");
    expect(lines.some((l) => l.startsWith("✓ daemon —"))).toBe(true);
    expect(lines.some((l) => l.startsWith("✗ api_key —"))).toBe(true);
    expect(lines.some((l) => l.startsWith("  fix: Open the wizard."))).toBe(true);
    expect(lines.some((l) => l.startsWith("– provider —"))).toBe(true);
  });

  it("redacts secret-shaped text in details", () => {
    const text = buildDoctorReport([
      { name: "daemon", status: "ok", detail: "github_pat_11ABCDEFG0abcdefghij leaked in" },
    ]);
    expect(text).not.toContain("github_pat_11ABCDEFG0abcdefghij");
    expect(text).toContain("[REDACTED]");
  });

  it("copyDoctorReport writes the redacted report via the clipboard seam", async () => {
    doctor.checks = [
      { name: "git", status: "ok", detail: "git 2.50.0" },
      { name: "api_key", status: "fail", detail: "no key", fix: "Store one." },
    ];
    let got = "";
    const ok = await copyDoctorReport({ writeText: async (t) => { got = t; } });
    expect(ok).toBe(true);
    expect(got).toContain("TST Desk doctor");
    expect(got).toContain("✗ api_key");
  });

  it("copyDoctorReport reports failure when the clipboard rejects", async () => {
    doctor.checks = [];
    const ok = await copyDoctorReport({ writeText: async () => { throw new Error("denied"); } });
    expect(ok).toBe(false);
  });
});
