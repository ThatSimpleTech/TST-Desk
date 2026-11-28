// ERR2B-05: a chat whose saved log had an unreadable line says so once,
// however often it is replayed, and names a newer version when it can.

import { afterEach, describe, expect, it } from "vitest";
import type { DaemonEventUnion } from "./protocol";
import { clearNotifications, notifyEvent, toasts } from "./notifications.svelte.js";
import { eventUnreadableNotice } from "./error-copy";

function unreadable(sessionId: string, seq: number, kind = ""): DaemonEventUnion {
  return { type: "event_unreadable", session_id: sessionId, seq, kind } as DaemonEventUnion;
}

afterEach(() => clearNotifications());

describe("event_unreadable", () => {
  it("tells once per chat across replays", () => {
    notifyEvent(unreadable("chat-a", 3));
    notifyEvent(unreadable("chat-a", 7));
    clearNotifications();
    notifyEvent(unreadable("chat-a", 3)); // the same chat replayed
    expect(toasts.filter((t) => t.title.startsWith("Part of this chat"))).toHaveLength(0);
    notifyEvent(unreadable("chat-b", 2));
    expect(toasts.filter((t) => t.title.startsWith("Part of this chat"))).toHaveLength(1);
  });

  it("names a newer version when the line still says what it was", () => {
    expect(eventUnreadableNotice("a_newer_event").body).toContain("newer version");
    expect(eventUnreadableNotice("").body).toContain("damaged");
  });
});
