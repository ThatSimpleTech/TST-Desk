// Memory-proposal store tests (TD-2402).
//
// Same bind-and-clear contract as approval-store (TD-1014). The store
// rides the connection fan-out and sends accept/reject back.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { Error, MemoryProposal } from "./protocol";

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

import {
  accept,
  bindMemoryProposal,
  pending,
  pendingForWorkspace,
  reject,
} from "./memory-proposal-store.svelte.js";

function proposal(overrides: Partial<MemoryProposal> = {}): MemoryProposal {
  return {
    type: "memory_proposal",
    seq: 21,
    session_id: "s1",
    proposal_id: "mp-1",
    files: [
      {
        action: "replace",
        path: ".tst/memory/MEMORY.md",
        diff: "--- a/.tst/memory/MEMORY.md\n+++ b/.tst/memory/MEMORY.md\n@@ -1 +1 @@\n-old\n+new\n",
        before: "old\n",
        after: "new\n",
      },
    ],
    ...overrides,
  };
}

function emit(event: MemoryProposal | Error): void {
  for (const handler of connection.handlers) handler(event);
}

beforeEach(() => {
  bindMemoryProposal(null);
  pending.splice(0, pending.length);
  connection.send.mockClear();
  connection.send.mockReturnValue(true);
  bindMemoryProposal("s1");
});

describe("event stream", () => {
  it("adds a card on memory_proposal", () => {
    emit(proposal());
    expect(pending).toHaveLength(1);
    expect(pending[0]?.proposalId).toBe("mp-1");
    expect(pending[0]?.files[0]?.path).toBe(".tst/memory/MEMORY.md");
    expect(pending[0]?.files[0]?.diff).toContain("+new");
  });

  it("replaces the card when a newer proposal arrives", () => {
    emit(proposal());
    emit(proposal({ proposal_id: "mp-2", seq: 22 }));
    expect(pending.map((p) => p.proposalId)).toEqual(["mp-2"]);
  });

  it("does not duplicate a replayed proposal with the same id", () => {
    emit(proposal());
    emit(proposal());
    expect(pending).toHaveLength(1);
  });

  it("ignores a proposal for a session that is not bound", () => {
    emit(proposal({ session_id: "s-other" }));
    expect(pending).toHaveLength(0);
  });

  it("clears the card when the daemon says no proposal is live", () => {
    emit(proposal());
    emit({
      type: "error",
      seq: 1,
      session_id: "s1",
      code: "no_memory_proposal",
      message: "No live memory proposal to accept, edit, or reject.",
    });
    expect(pending).toHaveLength(0);
  });
});

describe("session bind (TD-1014)", () => {
  it("binding another session drops leftover cards", () => {
    emit(proposal());
    bindMemoryProposal("s2");
    expect(pending).toHaveLength(0);
    emit(proposal({ session_id: "s2", proposal_id: "mp-2" }));
    expect(pending.map((p) => p.proposalId)).toEqual(["mp-2"]);
  });

  it("re-binding the session already shown keeps the card", () => {
    emit(proposal());
    bindMemoryProposal("s1");
    expect(pending).toHaveLength(1);
  });

  it("binding null unbinds and empties", () => {
    emit(proposal());
    bindMemoryProposal(null);
    emit(proposal());
    expect(pending).toHaveLength(0);
  });
});

describe("decision actions", () => {
  it("accept sends memory_accept and drops the card", () => {
    emit(proposal());
    accept(pending[0]!);
    expect(connection.send).toHaveBeenCalledWith({
      type: "memory_accept",
      session_id: "s1",
      proposal_id: "mp-1",
    });
    expect(pending).toHaveLength(0);
  });

  it("a second accept is not sent after the card has left", () => {
    emit(proposal());
    const card = pending[0]!;
    accept(card);
    connection.send.mockClear();
    accept(card);
    expect(connection.send).not.toHaveBeenCalled();
  });

  it("keeps the card when the socket refuses the send", () => {
    connection.send.mockReturnValue(false);
    emit(proposal());
    accept(pending[0]!);
    expect(pending).toHaveLength(1);
  });

  it("accept with edited drafts sends memory_edit", () => {
    emit(proposal());
    accept(pending[0]!, [{ path: ".tst/memory/MEMORY.md", content: "hand\n" }]);
    expect(connection.send).toHaveBeenCalledWith({
      type: "memory_edit",
      session_id: "s1",
      proposal_id: "mp-1",
      files: [{ path: ".tst/memory/MEMORY.md", content: "hand\n" }],
    });
    expect(pending).toHaveLength(0);
  });

  it("accept with unchanged drafts still sends memory_accept", () => {
    emit(proposal());
    accept(pending[0]!, [{ path: ".tst/memory/MEMORY.md", content: "new\n" }]);
    expect(connection.send).toHaveBeenCalledWith({
      type: "memory_accept",
      session_id: "s1",
      proposal_id: "mp-1",
    });
  });

  it("hosts the card on the Memory column for this workspace", () => {
    emit(proposal());
    const rows = [{ sessionId: "s1", workspacePath: "/ws" }];
    expect(pendingForWorkspace("/ws", rows).map((p) => p.proposalId)).toEqual(["mp-1"]);
    expect(pendingForWorkspace("/other", rows)).toEqual([]);
  });

  it("reject sends memory_reject and drops the card", () => {
    emit(proposal());
    reject(pending[0]!);
    expect(connection.send).toHaveBeenCalledWith({
      type: "memory_reject",
      session_id: "s1",
      proposal_id: "mp-1",
    });
    expect(pending).toHaveLength(0);
  });
});
