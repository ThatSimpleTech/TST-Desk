// Approval-model tests (TD-1007).

import { describe, it, expect } from "vitest";
import {
  approvalFromEvent,
  isDangerous,
  approveMessage,
  denyMessage,
} from "./approval";
import type { ApprovalRequest } from "./protocol";

function request(overrides: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {
    type: "approval_request",
    session_id: "s1",
    tool_call_id: "tc-1",
    tool_name: "fs_write",
    arguments: { path: "src/app.py" },
    decision_class: "B",
    summary: "Write src/app.py",
    reason: "decision class B requires approval",
    seq: 8,
    ...overrides,
  };
}

describe("approvalFromEvent", () => {
  it("maps every field the card renders", () => {
    const a = approvalFromEvent(request());
    expect(a).toEqual({
      sessionId: "s1",
      toolCallId: "tc-1",
      toolName: "fs_write",
      arguments: { path: "src/app.py" },
      decisionClass: "B",
      summary: "Write src/app.py",
      reason: "decision class B requires approval",
    });
  });
});

describe("isDangerous", () => {
  it("treats class C as dangerous and A/B as not", () => {
    expect(isDangerous(approvalFromEvent(request({ decision_class: "C" })))).toBe(true);
    expect(isDangerous(approvalFromEvent(request({ decision_class: "B" })))).toBe(false);
    expect(isDangerous(approvalFromEvent(request({ decision_class: "A" })))).toBe(false);
  });
});

describe("approveMessage / denyMessage", () => {
  const a = approvalFromEvent(request());

  it("builds an approve client message", () => {
    expect(approveMessage(a)).toEqual({
      type: "approve",
      session_id: "s1",
      tool_call_id: "tc-1",
    });
  });

  it("builds a deny message with an optional note", () => {
    expect(denyMessage(a, "not safe")).toEqual({
      type: "deny",
      session_id: "s1",
      tool_call_id: "tc-1",
      reason: "not safe",
    });
  });

  it("drops an empty or whitespace note to null", () => {
    expect(denyMessage(a)).toEqual({ type: "deny", session_id: "s1", tool_call_id: "tc-1", reason: null });
    expect(denyMessage(a, "   ").reason).toBeNull();
    expect(denyMessage(a, "").reason).toBeNull();
  });
});
