// @vitest-environment jsdom
//
// TD-5132: the diff view takes line comments, mounted for real. A number in
// the gutter starts a comment on that line, shift-click makes it a range,
// and the review bar sends every draft at once as one submit_line_comments.
// The drafts stay until the daemon's user_turn takes them; a refusal gives
// them back with the reason.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const connection = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return { handlers, sent: [] as unknown[], up: true };
});

vi.mock("./connection-status.svelte.js", () => ({
  onDaemonEvent: (handler: (event: unknown) => void) => {
    connection.handlers.add(handler);
    return () => connection.handlers.delete(handler);
  },
  sendToDaemon: (msg: unknown) => {
    if (!connection.up) return false;
    connection.sent.push(msg);
    return true;
  },
}));

import CommentableDiff from "./components/CommentableDiff.svelte";
import ReviewBar from "./components/ReviewBar.svelte";
import SentComments from "./components/SentComments.svelte";
import { bindReviewComments, review } from "./review-comments.svelte.js";
import type { SentComment } from "./review-comments";

const PATH = "/w/src/app.py";
const DIFF = [`--- a/${PATH}`, `+++ b/${PATH}`, "@@ -1,3 +1,4 @@", " one", "-two", "+TWO", "+extra", " three"].join(
  "\n",
);

function emit(event: unknown): void {
  for (const handler of connection.handlers) handler(event);
  flushSync();
}

let target: HTMLElement;
const mounted: ReturnType<typeof mount>[] = [];

beforeEach(() => {
  connection.sent.length = 0;
  connection.up = true;
  bindReviewComments(null);
  bindReviewComments("s1");
  target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(CommentableDiff, { target, props: { path: PATH, diff: DIFF } }));
  mounted.push(mount(ReviewBar, { target }));
  flushSync();
});

afterEach(() => {
  while (mounted.length > 0) unmount(mounted.pop()!);
  target.remove();
});

function byLabel(label: string): HTMLButtonElement {
  const found = target.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  expect(found, label).not.toBeNull();
  return found!;
}

function byText(label: string): HTMLButtonElement {
  const found = [...target.querySelectorAll("button")].find(
    (b) => (b.textContent ?? "").replace(/\s+/g, " ").trim() === label,
  );
  expect(found, label).toBeDefined();
  return found!;
}

function comment(first: number, last: number | null, text: string): void {
  byLabel(`Comment on line ${first}`).click();
  flushSync();
  if (last !== null) {
    byLabel(`Comment on line ${last}`).dispatchEvent(new MouseEvent("click", { bubbles: true, shiftKey: true }));
    flushSync();
  }
  const box = target.querySelector("textarea")!;
  box.value = text;
  box.dispatchEvent(new Event("input", { bubbles: true }));
  flushSync();
  byText("Add comment").click();
  flushSync();
}

describe("CommentableDiff + ReviewBar", () => {
  it("numbers only the lines of the file after the write", () => {
    const numbers = [...target.querySelectorAll("button.gutter")].map((b) => b.textContent);
    expect(numbers).toEqual(["1", "2", "3", "4"]);
  });

  it("sends every comment at once, anchored by file and line", () => {
    comment(2, 3, "name this better");
    comment(4, null, "and this");
    expect(target.textContent).toContain("2 comments ready");
    byText("Send comments to agent").click();
    flushSync();
    expect(connection.sent).toEqual([
      {
        type: "submit_line_comments",
        session_id: "s1",
        comments: [
          expect.objectContaining({ path: PATH, start_line: 2, end_line: 3, body: "name this better", lines: ["TWO", "extra"] }),
          expect.objectContaining({ path: PATH, start_line: 4, end_line: 4, body: "and this", lines: ["three"] }),
        ],
      },
    ]);
    expect(target.textContent).toContain("Sending 2 comments");
    const ids = (connection.sent[0] as { comments: { id: string }[] }).comments.map((c) => c.id);
    emit({
      type: "user_turn",
      session_id: "s1",
      turn_id: "t1",
      content: "Review comments",
      seq: 9,
      line_comments: ids.map((id) => ({ id, path: PATH, start_line: 1, end_line: 1, body: "x" })),
    });
    expect(review.drafts).toEqual([]);
    expect(review.sending).toBe(false);
  });

  it("a refusal keeps the drafts and shows why", () => {
    comment(1, null, "hm");
    byText("Send comments to agent").click();
    flushSync();
    emit({
      type: "error",
      code: "session_not_running",
      message: "This session is done.",
      session_id: "s1",
      in_reply_to: "submit_line_comments",
      seq: 0,
    });
    expect(review.drafts).toHaveLength(1);
    expect(review.sending).toBe(false);
    expect(target.textContent).toContain("This session is done.");
  });

  it("a comment with no text is not added", () => {
    byLabel("Comment on line 1").click();
    flushSync();
    byText("Add comment").click();
    flushSync();
    expect(review.drafts).toEqual([]);
    expect(target.textContent).toContain("Write the comment first.");
  });

  it("not connected: nothing is lost", () => {
    connection.up = false;
    comment(1, null, "later");
    byText("Send comments to agent").click();
    flushSync();
    expect(review.drafts).toHaveLength(1);
    expect(review.sending).toBe(false);
    expect(target.textContent).toContain("not connected");
  });
});

describe("SentComments", () => {
  it("shows each sent comment open or resolved", () => {
    const comments: SentComment[] = [
      { id: "a", path: PATH, start_line: 2, end_line: 3, body: "rename", turnSeq: 1, status: "resolved", resolvedSeq: 4 },
      { id: "b", path: PATH, start_line: 9, end_line: 9, body: "later", turnSeq: 1, status: "open", resolvedSeq: null },
    ];
    const el = document.createElement("div");
    const view = mount(SentComments, { target: el, props: { comments } });
    flushSync();
    const rows = [...el.querySelectorAll("li")].map((li) => li.textContent?.replace(/\s+/g, " ").trim());
    expect(rows).toEqual(["Resolved lines 2-3 rename", "Open line 9 later"]);
    unmount(view);
  });
});
