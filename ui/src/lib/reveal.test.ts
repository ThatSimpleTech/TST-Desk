// @vitest-environment jsdom
//
// RENDERSEC-02: a command with a bidi override displays in logical order on
// the approval card and in the activity timeline, with a visible marker.

import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

vi.mock("./connection-status.svelte.js", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  onDaemonEvent: () => () => {},
  sendToDaemon: () => true,
}));

import ApprovalCard from "./components/ApprovalCard.svelte";
import TimelineEntryRow from "./components/TimelineEntryRow.svelte";
import { approvalFromEvent } from "./approval";
import { hasHidden, reveal } from "./reveal";
import type { TimelineEntry } from "./timeline";

const RLO = String.fromCodePoint(0x202e);
const COMMAND = `rm -rf ${RLO}/emoh`;

describe("reveal", () => {
  it("shows a right-to-left override as a named marker", () => {
    expect(reveal(COMMAND)).toBe("rm -rf ⟨RLO U+202E⟩/emoh");
    expect(hasHidden(COMMAND)).toBe(true);
  });
  it("names zero-width and terminal codes, keeps newlines and tabs", () => {
    expect(reveal(`a${String.fromCodePoint(0x200b)}b\n\tc`)).toBe("a⟨ZWSP U+200B⟩b\n\tc");
    expect(reveal(`${String.fromCodePoint(0x1b)}[2J`)).toBe("⟨ESC U+001B⟩[2J");
  });
  it("leaves ordinary text alone", () => {
    const text = "npm test — 日本語 émigré";
    expect(reveal(text)).toBe(text);
    expect(hasHidden(text)).toBe(false);
  });
});

describe("where a person reads a command", () => {
  let app: ReturnType<typeof mount> | null = null;
  afterEach(() => {
    if (app) unmount(app);
    app = null;
    document.body.innerHTML = "";
  });

  it("the approval card shows the logical order and says so", () => {
    const approval = approvalFromEvent({
      type: "approval_request",
      session_id: "s1",
      tool_call_id: "tc-1",
      tool_name: "shell",
      arguments: { command: COMMAND },
      decision_class: "B",
      summary: `Run \`${COMMAND}\``,
      reason: "asked",
      seq: 1,
    });
    app = mount(ApprovalCard, { target: document.body, props: { approval } });
    flushSync();
    const text = document.body.textContent ?? "";
    expect(text).not.toContain(RLO);
    expect(text).toContain("rm -rf ⟨RLO U+202E⟩/emoh");
    expect(text).toContain("hidden or reordering characters");
    const label = document.querySelector("[data-approval-card]")?.getAttribute("aria-label") ?? "";
    expect(label).not.toContain(RLO);
  });

  it("the activity row shows it too", () => {
    const entry = {
      id: "e1",
      kind: "tool",
      title: COMMAND,
      preview: COMMAND,
      details: { command: COMMAND },
    } as unknown as TimelineEntry;
    app = mount(TimelineEntryRow, {
      target: document.body,
      props: { entry, expanded: true, ontoggle: () => {} },
    });
    flushSync();
    const text = document.body.textContent ?? "";
    expect(text).not.toContain(RLO);
    expect(text).toContain("⟨RLO U+202E⟩");
  });
});
