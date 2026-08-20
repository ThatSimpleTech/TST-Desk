// Memory-proposal model tests (TD-2402).

import { describe, it, expect } from "vitest";
import { acceptMessage, draftsDiffer, editMessage, proposalFromEvent, rejectMessage } from "./memory-proposal";
import type { MemoryProposal } from "./protocol";

function event(overrides: Partial<MemoryProposal> = {}): MemoryProposal {
  return {
    type: "memory_proposal",
    seq: 21,
    session_id: "s1",
    proposal_id: "mp-1",
    files: [
      {
        action: "replace",
        path: ".tst/memory/MEMORY.md",
        diff: "--- a/.tst/memory/MEMORY.md\n+++ b/.tst/memory/MEMORY.md\n@@ -1 +1 @@\n-old\n+durable: ruff\n",
        before: "old\n",
        after: "durable: ruff\n",
      },
    ],
    ...overrides,
  };
}

describe("proposalFromEvent", () => {
  it("maps every field the card renders", () => {
    const p = proposalFromEvent(event());
    expect(p).toEqual({
      sessionId: "s1",
      proposalId: "mp-1",
      files: event().files,
    });
  });
});

describe("acceptMessage / rejectMessage", () => {
  const p = proposalFromEvent(event());

  it("builds a memory_accept client message", () => {
    expect(acceptMessage(p)).toEqual({
      type: "memory_accept",
      session_id: "s1",
      proposal_id: "mp-1",
    });
  });

  it("builds a memory_reject client message", () => {
    expect(rejectMessage(p)).toEqual({
      type: "memory_reject",
      session_id: "s1",
      proposal_id: "mp-1",
    });
  });

  it("builds a memory_edit client message", () => {
    expect(editMessage(p, [{ path: ".tst/memory/MEMORY.md", content: "hand\n" }])).toEqual({
      type: "memory_edit",
      session_id: "s1",
      proposal_id: "mp-1",
      files: [{ path: ".tst/memory/MEMORY.md", content: "hand\n" }],
    });
  });
});

describe("draftsDiffer", () => {
  const p = proposalFromEvent(event());

  it("is false when drafts match the proposed after-bytes", () => {
    expect(draftsDiffer(p, [{ path: ".tst/memory/MEMORY.md", content: "durable: ruff\n" }])).toBe(
      false,
    );
  });

  it("is true when a draft changes or is emptied", () => {
    expect(draftsDiffer(p, [{ path: ".tst/memory/MEMORY.md", content: "hand\n" }])).toBe(true);
    expect(draftsDiffer(p, [{ path: ".tst/memory/MEMORY.md", content: "" }])).toBe(true);
  });
});
