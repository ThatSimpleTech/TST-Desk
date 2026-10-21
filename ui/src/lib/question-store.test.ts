// TD-5121: the question-card store. Cards follow the bound session, leave
// when the daemon says the question ended, and an answer for another
// computer's session goes only while that computer is connected.

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DaemonEventUnion, MachineSummary, QuestionRequest } from "./protocol";

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

import fixtures from "./protocol-fixtures.json";
import { answer, bindQuestions, canAnswer, decline, pending } from "./question-store.svelte.js";
import { machines, resetMachines } from "./machines.svelte.js";

const BASE = fixtures.question_request as QuestionRequest;

function request(sessionId: string, toolCallId = "tc-q"): QuestionRequest {
  return { ...BASE, session_id: sessionId, tool_call_id: toolCallId };
}

function emit(event: DaemonEventUnion): void {
  for (const handler of connection.handlers) handler(event);
}

const DRAFTS = [
  { selected: ["Postgres"], text: "" },
  { selected: ["Auth"], text: "" },
];

beforeEach(() => {
  bindQuestions(null);
  resetMachines();
  connection.send.mockClear();
  connection.send.mockReturnValue(true);
  bindQuestions("s1");
});

describe("question store", () => {
  it("adds one card per question_request of the bound session, replay included", () => {
    emit(request("s1"));
    emit(request("s1"));
    emit(request("s2", "tc-other"));
    expect(pending.map((c) => c.toolCallId)).toEqual(["tc-q"]);
  });

  it("a session switch drops the cards; the attach replay rebuilds them", () => {
    emit(request("s1"));
    bindQuestions("s2");
    expect(pending).toHaveLength(0);
    emit(request("s2", "tc-2"));
    expect(pending.map((c) => c.toolCallId)).toEqual(["tc-2"]);
  });

  it("question_answered, the call's tool_result or turn_complete take the card away", () => {
    emit(request("s1"));
    emit({ type: "question_answered", session_id: "s1", tool_call_id: "tc-q", outcome: "declined", answers: [], seq: 2 });
    expect(pending).toHaveLength(0);
    emit(request("s1"));
    emit({ type: "turn_complete", session_id: "s1", tokens: 0, cost: 0, tier: "brain", duration: 0, seq: 3 } as DaemonEventUnion);
    expect(pending).toHaveLength(0);
  });

  it("answers once, and keeps the card until the daemon says it ended", () => {
    emit(request("s1"));
    answer(pending[0]!, DRAFTS);
    answer(pending[0]!, DRAFTS);
    decline(pending[0]!);
    expect(connection.send).toHaveBeenCalledTimes(1);
    expect(connection.send).toHaveBeenCalledWith({
      type: "answer_question",
      session_id: "s1",
      tool_call_id: "tc-q",
      answers: DRAFTS,
    });
    expect(pending[0]!.sent).toBe(true);
  });

  it("a question the daemon no longer holds leaves", () => {
    emit(request("s1"));
    decline(pending[0]!);
    emit({ type: "error", code: "no_pending_question", message: "gone", session_id: "s1", in_reply_to: "answer_question", seq: 1 });
    expect(pending).toHaveLength(0);
  });
});

describe("questions for another computer's session", () => {
  const MCH = "mch_0123456789ab";
  const REMOTE = `r1:${MCH}`;

  function machineStatus(status: MachineSummary["status"]): void {
    machines.list = [
      {
        id: MCH,
        name: "tower",
        host: null,
        port: null,
        status,
        last_seen: null,
        paired_at: "2026-10-14T12:00:00Z",
        same_computer: false,
        detail: null,
      },
    ];
  }

  beforeEach(() => {
    bindQuestions(REMOTE);
    machineStatus("connected");
  });

  it("sends nothing while that computer is not connected", () => {
    emit(request(REMOTE));
    machineStatus("unreachable");
    expect(canAnswer(pending[0]!)).toBe(false);
    answer(pending[0]!, DRAFTS);
    decline(pending[0]!);
    expect(connection.send).not.toHaveBeenCalled();
  });

  it("an answer the hub could not deliver can be sent again", () => {
    emit(request(REMOTE));
    answer(pending[0]!, DRAFTS);
    emit({
      type: "error",
      seq: 1,
      session_id: REMOTE,
      machine: MCH,
      code: "machine_unreachable",
      message: "tower isn't reachable.",
      in_reply_to: "answer_question",
    });
    expect(pending[0]!.sent).toBe(false);
    expect(pending[0]!.notice).toBe("tower isn't reachable.");
    answer(pending[0]!, DRAFTS);
    expect(connection.send).toHaveBeenCalledTimes(2);
  });
});
