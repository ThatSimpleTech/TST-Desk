// TD-5125: the "Restore files to here" dialog's state, without a socket.

import { describe, expect, it } from "vitest";
import fixtures from "./protocol-fixtures.json";
import type { DaemonEventUnion, FilesRestored, FilesRestorePreview } from "./protocol";
import {
  createRewindState,
  isRewindError,
  openRewind,
  reduceRewind,
  restoreCount,
  restoredSummary,
  restoreRequest,
  toggleTicked,
} from "./rewind";

const PREVIEW = fixtures.files_restore_preview as FilesRestorePreview;
const EMPTY = fixtures.files_restore_preview_empty as FilesRestorePreview;
const RESTORED = fixtures.files_restored as FilesRestored;

function opened(resend: string | null = null) {
  const state = createRewindState();
  const ask = openRewind(state, "sess-1", "turn-1", 2, resend);
  return { state, ask };
}

describe("rewind dialog state", () => {
  it("asks the daemon for the turn's preview and waits for it", () => {
    const { state, ask } = opened();
    expect(ask).toEqual({ type: "preview_restore_files", session_id: "sess-1", turn_id: "turn-1" });
    expect(state.phase).toBe("loading");
    reduceRewind(state, PREVIEW);
    expect(state.phase).toBe("ready");
    expect(state.preview?.plan_id).toBe(PREVIEW.plan_id);
  });

  it("ignores another turn's or another session's preview", () => {
    const { state } = opened();
    reduceRewind(state, { ...PREVIEW, turn_id: "turn-9" });
    reduceRewind(state, { ...PREVIEW, session_id: "sess-2" });
    expect(state.phase).toBe("loading");
  });

  it("restores without the hand-edited files unless they are ticked", () => {
    const { state } = opened();
    reduceRewind(state, PREVIEW);
    expect(restoreCount(state)).toBe(3);
    toggleTicked(state, "notes.md");
    expect(restoreCount(state)).toBe(4);
    toggleTicked(state, "notes.md");
    const request = restoreRequest(state);
    expect(request).toEqual({
      type: "restore_files",
      session_id: "sess-1",
      turn_id: "turn-1",
      plan_id: PREVIEW.plan_id,
      include_hand_edited: [],
    });
    expect(state.phase).toBe("restoring");
  });

  it("sends a ticked hand-edited file, and nothing that is not one", () => {
    const { state } = opened();
    reduceRewind(state, PREVIEW);
    toggleTicked(state, "notes.md");
    toggleTicked(state, "src/app.py"); // not hand-edited: never named
    expect(restoreRequest(state)?.include_hand_edited).toEqual(["notes.md"]);
  });

  it("has nothing to send when nothing changed since the turn", () => {
    const { state } = opened();
    reduceRewind(state, { ...EMPTY, turn_id: "turn-1" });
    expect(state.phase).toBe("ready");
    expect(restoreRequest(state)).toBeNull();
  });

  it("finishes on the turn's files_restored, and resends an edit then", () => {
    const plain = opened().state;
    reduceRewind(plain, PREVIEW);
    restoreRequest(plain);
    expect(reduceRewind(plain, RESTORED)).toBeNull();
    expect(plain.phase).toBe("done");

    const edit = opened("try again, smaller").state;
    reduceRewind(edit, PREVIEW);
    restoreRequest(edit);
    expect(reduceRewind(edit, RESTORED)).toBe("fork");
  });

  it("shows the daemon's refusal of its own request in the dialog", () => {
    const { state } = opened();
    const refusal: DaemonEventUnion = {
      type: "error",
      seq: 1,
      session_id: "sess-1",
      code: "turn_in_progress",
      message: "Files can be restored when the turn ends.",
      in_reply_to: "preview_restore_files",
    };
    expect(isRewindError(state, refusal)).toBe(true);
    reduceRewind(state, refusal);
    expect(state.phase).toBe("error");
    expect(state.error).toBe("Files can be restored when the turn ends.");
    const other = { ...refusal, in_reply_to: "fork_from" };
    expect(isRewindError(state, other)).toBe(false);
  });

  it("says what came back, and what was left alone", () => {
    expect(restoredSummary(RESTORED)).toBe(
      "Put back 2 files, removed 1 file. Left 1 file you changed by hand untouched.",
    );
  });
});
