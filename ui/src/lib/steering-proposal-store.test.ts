// Steering-proposal store tests (TD-5140).
//
// The store rides the connection fan-out. A card leaves only when the log
// says the proposal was decided or replaced; a refusal stays on the card.

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Error, SteeringDecision, SteeringProposal } from "./protocol";

const connection = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return { handlers, send: vi.fn((_msg: unknown) => true) };
});

vi.mock("./connection-status.svelte.js", () => ({
  onDaemonEvent: (handler: (event: unknown) => void) => {
    connection.handlers.add(handler);
    return () => connection.handlers.delete(handler);
  },
  sendToDaemon: connection.send,
}));

import {
  accept,
  bindSteeringProposal,
  pending,
  reject,
  saveEdit,
} from "./steering-proposal-store.svelte.js";
import { FRESH_DIFF_NOTICE } from "./steering-proposal";

function proposal(overrides: Partial<SteeringProposal> = {}): SteeringProposal {
  return {
    type: "steering_proposal",
    seq: 30,
    session_id: "s1",
    proposal_id: "sp-1",
    tool_call_id: "call-1",
    replaces: null,
    autonomy: false,
    files: [
      {
        action: "replace",
        path: "AGENTS.md",
        diff: "--- a/AGENTS.md\n+++ b/AGENTS.md\n@@ -1 +1,2 @@\n # P\n+- Run ruff.\n",
        before: "# P\n",
        after: "# P\n- Run ruff.\n",
      },
    ],
    ...overrides,
  };
}

function decision(overrides: Partial<SteeringDecision> = {}): SteeringDecision {
  return {
    type: "steering_decision",
    seq: 31,
    session_id: "s1",
    proposal_id: "sp-1",
    decision: "accepted",
    files: proposal().files,
    ...overrides,
  };
}

function refusal(code: string, message: string, inReplyTo = "steering_accept"): Error {
  return { type: "error", seq: 1, session_id: "s1", code, message, in_reply_to: inReplyTo };
}

function emit(event: SteeringProposal | SteeringDecision | Error): void {
  for (const handler of connection.handlers) handler(event);
}

beforeEach(() => {
  bindSteeringProposal(null);
  connection.send.mockClear();
  connection.send.mockReturnValue(true);
  bindSteeringProposal("s1");
});

describe("event stream", () => {
  it("adds a card per proposal and ignores other sessions", () => {
    emit(proposal());
    emit(proposal({ proposal_id: "sp-2", files: [{ ...proposal().files[0]!, path: "CLAUDE.md" }] }));
    emit(proposal({ session_id: "s-other", proposal_id: "sp-3" }));
    expect(pending.map((p) => [p.proposalId, p.file.path])).toEqual([
      ["sp-1", "AGENTS.md"],
      ["sp-2", "CLAUDE.md"],
    ]);
    expect(pending[0]?.notice).toBeNull();
  });

  it("does not duplicate a replayed proposal", () => {
    emit(proposal());
    emit(proposal());
    expect(pending).toHaveLength(1);
  });

  it("a decision closes the card, and a replay keeps it closed", () => {
    emit(proposal());
    emit(decision());
    expect(pending).toHaveLength(0);
    emit(proposal());
    expect(pending).toHaveLength(0);
  });

  it("a fresh diff replaces the stale card and says why", () => {
    emit(proposal());
    emit(proposal({ proposal_id: "sp-2", replaces: "sp-1" }));
    expect(pending.map((p) => p.proposalId)).toEqual(["sp-2"]);
    expect(pending[0]?.notice).toBe(FRESH_DIFF_NOTICE);
  });

  it("marks a proposal parked by an unattended run", () => {
    emit(proposal({ autonomy: true }));
    expect(pending[0]?.autonomy).toBe(true);
  });
});

describe("decisions", () => {
  it("Accept sends steering_accept and keeps the card until the decision", () => {
    emit(proposal());
    accept(pending[0]!);
    expect(connection.send).toHaveBeenCalledWith({
      type: "steering_accept",
      session_id: "s1",
      proposal_id: "sp-1",
    });
    expect(pending).toHaveLength(1);
    emit(decision());
    expect(pending).toHaveLength(0);
  });

  it("Save sends the edited text as steering_edit", () => {
    emit(proposal());
    saveEdit(pending[0]!, "# Mine\n");
    expect(connection.send).toHaveBeenCalledWith({
      type: "steering_edit",
      session_id: "s1",
      proposal_id: "sp-1",
      content: "# Mine\n",
    });
  });

  it("Reject sends steering_reject", () => {
    emit(proposal());
    reject(pending[0]!);
    expect(connection.send).toHaveBeenCalledWith({
      type: "steering_reject",
      session_id: "s1",
      proposal_id: "sp-1",
    });
  });

  it("a refusal stays on the card as a notice", () => {
    emit(proposal());
    accept(pending[0]!);
    emit(refusal("loopback_only", "Only the computer itself can do that."));
    expect(pending[0]?.notice).toBe("Only the computer itself can do that.");
  });

  it("a refusal from the peer gate with no session id still lands", () => {
    emit(proposal());
    accept(pending[0]!);
    emit({ ...refusal("loopback_only", "Only the computer itself can do that."), session_id: null });
    expect(pending[0]?.notice).toBe("Only the computer itself can do that.");
  });

  it("a proposal the daemon no longer holds is dropped", () => {
    emit(proposal());
    accept(pending[0]!);
    emit(refusal("no_steering_proposal", "That steering proposal is no longer open."));
    expect(pending).toHaveLength(0);
  });

  it("an error for some other verb leaves the cards alone", () => {
    emit(proposal());
    accept(pending[0]!);
    emit(refusal("no_memory_proposal", "No live memory proposal.", "memory_accept"));
    expect(pending[0]?.notice).toBeNull();
  });

  it("a send that cannot go out says so", () => {
    emit(proposal());
    connection.send.mockReturnValue(false);
    accept(pending[0]!);
    expect(pending[0]?.notice).toContain("Not sent");
  });
});

describe("session bind (TD-1014)", () => {
  it("binding another session drops leftover cards", () => {
    emit(proposal());
    bindSteeringProposal("s2");
    expect(pending).toHaveLength(0);
    emit(proposal({ session_id: "s2", proposal_id: "sp-9" }));
    expect(pending.map((p) => p.proposalId)).toEqual(["sp-9"]);
  });

  it("re-binding the session already shown keeps the cards", () => {
    emit(proposal());
    bindSteeringProposal("s1");
    expect(pending).toHaveLength(1);
  });
});
