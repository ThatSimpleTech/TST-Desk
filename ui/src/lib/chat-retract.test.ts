// A stalled reply withdrawn and asked again (TD-5159). The daemon tags each
// model response's deltas with a `response_id`, and `assistant_retract` takes
// one back off the row: the user ends up with one reply, not the stalled half
// followed by the whole. Before this story the pane dropped the retract and
// glued the two together.

import { describe, expect, it } from "vitest";
import { createChatState, createChatStore, type ChatDeps } from "./chat-store";
import type { DaemonEventUnion } from "./protocol";
import { entryTone } from "./entry-view";
import { eventToEntry } from "./timeline";

let seq = 1;

function ev(event: Record<string, unknown>): DaemonEventUnion {
  seq += 1;
  return { session_id: "s1", seq, ...event } as DaemonEventUnion;
}

const delta = (text: string, responseId?: string): DaemonEventUnion =>
  ev({ type: "assistant_delta", delta: text, ...(responseId ? { response_id: responseId } : {}) });
const thinking = (text: string, responseId: string): DaemonEventUnion =>
  ev({ type: "assistant_reasoning", delta: text, response_id: responseId });
const retract = (responseId: string): DaemonEventUnion =>
  ev({ type: "assistant_retract", response_id: responseId, reason: "stream_stalled" });
const toolCall = (): DaemonEventUnion =>
  ev({ type: "tool_call", tool_call_id: "c1", name: "echo", arguments: { message: "hi" }, decision_class: "A" });
const toolResult = (): DaemonEventUnion =>
  ev({ type: "tool_result", tool_call_id: "c1", status: "success", output: "Echo: hi" });
const turnComplete = (): DaemonEventUnion =>
  ev({ type: "turn_complete", tokens: 0, cost: 0, tier: "brain", duration: 1, failed: false, error_code: null });

function bound() {
  const deps: ChatDeps = { send: () => true, attach: () => {}, detach: () => {} };
  const state = createChatState();
  const store = createChatStore(deps, state);
  store.applyEvent({
    type: "session_list",
    seq: 1,
    sessions: [
      {
        session_id: "s1",
        workspace_path: "/workspace",
        state: "idle",
        created_at: "2026-10-20T10:00:00Z",
        updated_at: "2026-10-20T10:00:00Z",
        event_count: 0,
        archived: false,
        starred: false,
      },
    ],
  });
  store.applyEvent(ev({ type: "user_turn", turn_id: "t1", content: "fix it" }));
  const apply = (...events: DaemonEventUnion[]) => events.forEach((e) => store.applyEvent(e));
  const reply = () => {
    const rows = state.messages.filter((m) => m.role === "assistant");
    expect(rows).toHaveLength(1);
    return rows[0];
  };
  return { state, apply, reply };
}

describe("a reply withdrawn after a stall", () => {
  it("is replaced by the retry: one reply on the row", () => {
    const { state, apply, reply } = bound();
    apply(delta("The fix is to dele", "r1"), retract("r1"));

    expect(reply().text).toBe("");
    // Until the retry's first token the Working line says what happened.
    expect(state.awaitingFirstToken).toBe(true);
    expect(state.retryingReply).toBe(true);

    apply(delta("Done.", "r2"));
    expect(state.awaitingFirstToken).toBe(false);
    expect(state.retryingReply).toBe(false);

    apply(turnComplete());
    expect(reply().text).toBe("Done.");
    expect(reply().parts).toEqual([{ kind: "text", text: "Done." }]);
    expect(reply().complete).toBe(true);
  });

  it("keeps a tool that already ran and the text before it", () => {
    const { apply, reply } = bound();
    apply(
      delta("Checking. ", "r1"),
      toolCall(),
      toolResult(),
      delta("It says hi, so the fi", "r2"),
      retract("r2"),
      delta("It says hi.", "r3"),
      turnComplete(),
    );

    const row = reply();
    expect(row.text).toBe("Checking. It says hi.");
    expect(row.parts?.map((p) => (p.kind === "text" ? p.text : `tool:${p.tool.name}`))).toEqual([
      "Checking. ",
      "tool:echo",
      "It says hi.",
    ]);
    expect(row.tools).toHaveLength(1);
    expect(row.tools?.[0].status).toBe("success");
  });

  it("takes the stalled response's thinking too", () => {
    const { apply, reply } = bound();
    apply(thinking("Let me look", "r1"), retract("r1"));
    expect(reply().reasoning).toBeUndefined();
    expect(reply().reasoningStartedAt).toBeUndefined();

    apply(thinking("Looked.", "r2"), delta("Done.", "r2"), turnComplete());
    expect(reply().reasoning).toBe("Looked.");
    expect(reply().text).toBe("Done.");
  });

  it("leaves an earlier response's thinking and its timing alone", () => {
    const { apply, reply } = bound();
    apply(thinking("First.", "r1"), delta("Checking. ", "r1"), toolCall());
    const ms = reply().reasoningMs;
    expect(ms).toBeDefined();

    apply(thinking(" Second.", "r2"), retract("r2"));
    expect(reply().reasoning).toBe("First.");
    expect(reply().reasoningMs).toBe(ms);
    expect(reply().text).toBe("Checking. ");
  });

  it("never withdraws text the daemon wrote without a response id", () => {
    const { apply, reply } = bound();
    // A progress note, then a response appended to the same text part.
    apply(delta("Next: tests. "), delta("Running the te", "r2"), retract("r2"));
    expect(reply().text).toBe("Next: tests. ");
    expect(reply().parts).toEqual([{ kind: "text", text: "Next: tests. " }]);
  });

  it("changes nothing for a response the row never showed", () => {
    const { apply, reply } = bound();
    apply(delta("hello", "r1"), retract("r9"));
    expect(reply().text).toBe("hello");
  });

  it("is a timeline entry that says why the text went, as a warning", () => {
    const entry = eventToEntry(retract("r1"));
    expect(entry?.kind).toBe("retry");
    expect(entry?.title).toBe("Provider went quiet");
    expect(entry?.details).toMatchObject({ reason: "stream_stalled", response_id: "r1" });
    if (entry) expect(entryTone(entry)).toBe("warning");
  });
});
