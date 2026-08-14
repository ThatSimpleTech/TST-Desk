// Tests for the decisions-ledger store (TD-1202).
//
// The store reads the daemon stream through connection-status and the active
// session/workspace through session-status, so the tests mock exactly those
// seams and drive real decision_logged events through the reducer.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => {
  const state = {
    handler: null as ((e: DaemonEventUnion) => void) | null,
    session: { sessionId: "s1" as string | null, workspacePath: "/home/u/proj" as string | null },
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
  sendToDaemon: (_msg: ClientMessageUnion) => true,
}));

vi.mock("./session-status.svelte.js", () => ({
  get session() {
    return mocks.session;
  },
}));

import {
  decisions,
  startDecisions,
  resetDecisions,
  filteredDecisions,
  setClassFilter,
  openDecisions,
  closeDecisions,
  ledgerPath,
  LEDGER_RELATIVE_PATH,
} from "./decisions.svelte.js";

function emit(event: DaemonEventUnion): void {
  mocks.handler?.(event);
}

const logged = (
  seq: number,
  decisionClass: "A" | "B" | "C",
  commit: string | null,
  sessionId = "s1",
): DaemonEventUnion =>
  ({
    type: "decision_logged",
    session_id: sessionId,
    seq,
    decision_class: decisionClass,
    what: `chose thing ${seq}`,
    why: `because ${seq}`,
    commit,
  }) as DaemonEventUnion;

beforeEach(() => {
  resetDecisions();
  mocks.session.sessionId = "s1";
  mocks.session.workspacePath = "/home/u/proj";
});

describe("stream reduce", () => {
  it("collects decision_logged events for the active session", () => {
    startDecisions();
    emit(logged(1, "A", "abc123"));
    emit(logged(2, "B", null));
    expect(decisions.rows).toHaveLength(2);
    expect(decisions.rows[0].what).toContain("chose thing 1");
  });

  it("ignores other sessions' decisions", () => {
    startDecisions();
    emit(logged(1, "A", "abc123", "other-session"));
    expect(decisions.rows).toHaveLength(0);
  });

  it("ignores everything until a session is active", () => {
    mocks.session.sessionId = null;
    startDecisions();
    emit(logged(1, "A", "abc123"));
    expect(decisions.rows).toHaveLength(0);
  });
});

describe("revert command (AC: copyable revert per entry)", () => {
  it("derives git revert from the attributed commit", () => {
    startDecisions();
    emit(logged(1, "A", "deadbeef"));
    expect(decisions.rows[0].undoCommand).toBe("git revert deadbeef");
  });

  it("offers none for a Class B entry without a commit", () => {
    startDecisions();
    emit(logged(1, "B", null));
    expect(decisions.rows[0].undoCommand).toBeNull();
  });
});

describe("class filter (AC)", () => {
  it("narrows rows to the chosen class — the under-a-minute Class A scan", () => {
    startDecisions();
    emit(logged(1, "A", "abc123"));
    emit(logged(2, "B", null));
    emit(logged(3, "A", "def456"));
    setClassFilter("A");
    const rows = filteredDecisions();
    expect(rows).toHaveLength(2);
    expect(rows.every((r) => r.decisionClass === "A")).toBe(true);
    setClassFilter(null);
    expect(filteredDecisions()).toHaveLength(3);
  });
});

describe("panel open/close and ledger path", () => {
  it("opens, closes", () => {
    openDecisions();
    expect(decisions.open).toBe(true);
    closeDecisions();
    expect(decisions.open).toBe(false);
  });

  it("names the workspace ledger file (the link-out target)", () => {
    expect(ledgerPath()).toBe(`/home/u/proj/${LEDGER_RELATIVE_PATH}`);
    expect(LEDGER_RELATIVE_PATH).toBe(".tst/autonomy/DECISIONS.md");
  });

  it("no workspace, no path", () => {
    mocks.session.workspacePath = null;
    expect(ledgerPath()).toBeNull();
  });
});
