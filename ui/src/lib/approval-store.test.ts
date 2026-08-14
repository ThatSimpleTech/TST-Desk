// Approval-store behavior tests (TD-1007).
//
// The store is a thin runes layer over approval.ts: it rides the connection
// fan-out and sends decisions back. These tests mock the connection module,
// drive the store with protocol-shaped events the way the daemon stream
// does, and assert what lands on the wire — including the replay shapes a
// reattach produces (TD-1003's from_seq contract).

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ApprovalRequest, ToolResult } from "./protocol";

// Captures the handler the store registers and every message it sends.
const connection = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return {
    handlers,
    send: vi.fn((_msg: unknown) => true),
  };
});

vi.mock("./connection-status.svelte.js", () => ({
  onDaemonEvent: (handler: (event: unknown) => void) => {
    connection.handlers.add(handler);
    return () => {
      connection.handlers.delete(handler);
    };
  },
  sendToDaemon: connection.send,
}));

import { alwaysAllow, approve, deny, pending } from "./approval-store.svelte.js";

function request(toolCallId: string, overrides: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {
    type: "approval_request",
    session_id: "s1",
    tool_call_id: toolCallId,
    tool_name: "fs_write",
    arguments: { path: "src/app.py" },
    decision_class: "B",
    summary: "Write src/app.py",
    reason: "decision class B requires approval",
    seq: 8,
    ...overrides,
  };
}

function result(toolCallId: string, errorCode: string | null = null): ToolResult {
  return {
    type: "tool_result",
    session_id: "s1",
    tool_call_id: toolCallId,
    status: errorCode !== null ? "error" : "success",
    output: "",
    truncated: false,
    error_code: errorCode,
    seq: 9,
  };
}

function emit(event: ApprovalRequest | ToolResult): void {
  for (const handler of connection.handlers) handler(event);
}

beforeEach(() => {
  pending.splice(0, pending.length);
  connection.send.mockClear();
});

describe("event stream", () => {
  it("adds a pending card on approval_request", () => {
    emit(request("tc-1"));
    expect(pending).toHaveLength(1);
    expect(pending[0].toolCallId).toBe("tc-1");
    expect(pending[0].summary).toBe("Write src/app.py");
    expect(pending[0].decisionClass).toBe("B");
  });

  it("tracks multiple concurrent approvals independently", () => {
    emit(request("tc-1"));
    emit(request("tc-2"));
    expect(pending.map((p) => p.toolCallId)).toEqual(["tc-1", "tc-2"]);
  });

  it("removes the card when its tool_result arrives, approved or denied", () => {
    emit(request("tc-1"));
    emit(request("tc-2"));
    emit(result("tc-1"));
    expect(pending.map((p) => p.toolCallId)).toEqual(["tc-2"]);
    emit(result("tc-2", "approval_denied"));
    expect(pending).toHaveLength(0);
  });

  it("ignores a tool_result for a call that is not pending", () => {
    emit(request("tc-1"));
    emit(result("tc-other"));
    expect(pending.map((p) => p.toolCallId)).toEqual(["tc-1"]);
  });

  it("nets out a resolved approval replayed on reattach", () => {
    // A from_seq replay delivers the request and its resolving result in
    // order; the card must not linger after the pair.
    emit(request("tc-1"));
    emit(result("tc-1"));
    expect(pending).toHaveLength(0);
  });

  it("re-surfaces a still-pending approval replayed on reattach", () => {
    // The daemon logs approval_request so a client attaching after the fact
    // (or a UI that reloaded mid-decision) sees the request without a
    // resolving result — the card must come back.
    emit(request("tc-1"));
    expect(pending.map((p) => p.toolCallId)).toEqual(["tc-1"]);
  });
});

describe("decision actions", () => {
  it("approve sends the approve client message", () => {
    approve(approvalShim("tc-1"));
    expect(connection.send).toHaveBeenCalledWith({
      type: "approve",
      session_id: "s1",
      tool_call_id: "tc-1",
    });
  });

  it("deny sends the note back to the model, null when blank", () => {
    deny(approvalShim("tc-1"), "not safe");
    expect(connection.send).toHaveBeenCalledWith({
      type: "deny",
      session_id: "s1",
      tool_call_id: "tc-1",
      reason: "not safe",
    });
    connection.send.mockClear();
    deny(approvalShim("tc-1"), "   ");
    expect(connection.send).toHaveBeenCalledWith({
      type: "deny",
      session_id: "s1",
      tool_call_id: "tc-1",
      reason: null,
    });
  });

  it("always allow sends the always_allow client message", () => {
    alwaysAllow(approvalShim("tc-1"));
    expect(connection.send).toHaveBeenCalledWith({
      type: "always_allow",
      session_id: "s1",
      tool_call_id: "tc-1",
    });
  });
});

/** A store-level PendingApproval without going through the event stream. */
function approvalShim(toolCallId: string) {
  return {
    sessionId: "s1",
    toolCallId,
    toolName: "fs_write",
    arguments: { path: "src/app.py" },
    decisionClass: "B" as const,
    summary: "Write src/app.py",
    reason: "decision class B requires approval",
    proposedAlwaysAllow: null,
  };
}
