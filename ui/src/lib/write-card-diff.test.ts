// @vitest-environment jsdom
//
// APPROVE-03: a write's approval card shows the change it would make, from
// the diff the daemon computed, with a one-line headline above it.

import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

vi.mock("./connection-status.svelte.js", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  onDaemonEvent: () => () => {},
  sendToDaemon: () => true,
}));

import ApprovalCard from "./components/ApprovalCard.svelte";
import { approvalFromEvent, diffHeadline } from "./approval";
import type { ApprovalRequest } from "./protocol";

const NEW_FILE = "--- /dev/null\n+++ b/notes.md\n@@ -0,0 +1,2 @@\n+one\n+two\n";
const EDIT = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 2\n y = 1\n";

describe("diffHeadline", () => {
  it("names a new file and counts its lines", () => {
    expect(diffHeadline(NEW_FILE)).toBe("New file, 2 lines");
  });
  it("counts changed lines, not context or headers", () => {
    expect(diffHeadline(EDIT)).toBe("Changes 2 lines");
  });
  it("says when nothing changes", () => {
    expect(diffHeadline("--- a/x\n+++ b/x\n")).toBe("No change");
  });
});

describe("the write card", () => {
  let app: ReturnType<typeof mount> | null = null;
  afterEach(() => {
    if (app) unmount(app);
    app = null;
    document.body.innerHTML = "";
  });

  it("shows the daemon's diff under its headline", () => {
    const event: ApprovalRequest = {
      type: "approval_request",
      session_id: "s1",
      tool_call_id: "tc-1",
      tool_name: "fs_write",
      arguments: { path: "notes.md", content: "one\ntwo\n" },
      diff: NEW_FILE,
      decision_class: "B",
      summary: "Write notes.md",
      reason: "decision class B requires approval",
      seq: 1,
    };
    const approval = approvalFromEvent(event);
    expect(approval.diff).toBe(NEW_FILE);
    app = mount(ApprovalCard, { target: document.body, props: { approval } });
    flushSync();
    expect(document.querySelector(".diff-head")?.textContent).toBe("New file, 2 lines");
    expect(document.body.textContent).toContain("+one");
  });
});
