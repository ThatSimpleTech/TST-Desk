// The pane's own binds, and the archived flag they keep (TD-5201).
//
// A chat opened from the archived shelf is archived already, so a later list
// that says so must not send the pane away; one the person archives while it
// is open still does (TD-1715). And when the pane binds by itself (the newest
// live chat at startup, the next one after an archive) the title bar and the
// task strip have to follow it, which only a click used to make them do.

import { describe, it, expect } from "vitest";
import { createChatState, createChatStore, type ChatDeps } from "./chat-store";
import type { DaemonEventUnion, SessionSummary } from "./protocol";

const MCH = "mch_0123456789ab";
const REMOTE = `abc123:${MCH}`;

type Row = { id: string; updated: string; archived?: boolean };

function list(rows: Row[]): DaemonEventUnion {
  return {
    type: "session_list",
    seq: 1,
    sessions: rows.map((row) => ({
      session_id: row.id,
      workspace_path: `/work/${row.id}`,
      state: "idle",
      created_at: row.updated,
      updated_at: row.updated,
      event_count: 0,
      archived: row.archived ?? false,
      starred: false,
    })),
  };
}

function store() {
  const followed: SessionSummary[] = [];
  const deps: ChatDeps = {
    send: () => true,
    attach: () => {},
    detach: () => {},
    onAutoBind: (summary) => followed.push(summary),
    drafts: { restore: () => "", schedule: () => {}, flush: () => {} },
  };
  const state = createChatState();
  return { store: createChatStore(deps, state), state, followed };
}

const OWN: Row = { id: "own", updated: "2026-10-30T10:00:00Z" };
const FILED: Row = { id: REMOTE, updated: "2026-10-30T09:00:00Z", archived: true };

describe("the pane's own binds (TD-5201)", () => {
  it("keeps another computer's archived chat through a hub restart", () => {
    const { store: chat, state } = store();
    chat.applyEvent(list([OWN, FILED]));
    chat.selectSession(REMOTE, "idle");
    chat.applyEvent(list([OWN, FILED]));
    chat.reconnected();
    // The restarted hub's first list has no remote rows yet.
    chat.applyEvent(list([OWN]));
    expect(state.sessionId).toBe(REMOTE);
    chat.applyEvent(list([OWN, FILED]));
    expect(state.sessionId).toBe(REMOTE);
  });

  it("still moves off a chat archived while it is open", () => {
    const { store: chat, state } = store();
    chat.applyEvent(list([{ id: "a", updated: "2026-10-30T11:00:00Z" }, OWN]));
    expect(state.sessionId).toBe("a");
    chat.applyEvent(list([{ id: "a", updated: "2026-10-30T11:00:00Z", archived: true }, OWN]));
    expect(state.sessionId).toBe("own");
  });

  it("tells the title bar about every bind it makes by itself, never a click's", () => {
    const { store: chat, followed } = store();
    chat.applyEvent(list([{ id: "a", updated: "2026-10-30T11:00:00Z" }, OWN]));
    chat.selectSession("own", "idle");
    chat.applyEvent(list([{ id: "a", updated: "2026-10-30T11:00:00Z" }, OWN]));
    chat.applyEvent(list([{ id: "a", updated: "2026-10-30T11:00:00Z" }, { ...OWN, archived: true }]));
    expect(followed.map((s) => [s.session_id, s.workspace_path])).toEqual([
      ["a", "/work/a"],
      ["a", "/work/a"],
    ]);
  });
});
