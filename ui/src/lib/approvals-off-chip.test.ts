// @vitest-environment jsdom
//
// APPROVE-05: while approvals are off, the title bar says so, and the chip
// opens the setting.

import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

vi.mock("./connection-status.svelte.js", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  sendToDaemon: () => true,
}));

import TitleBar from "./components/TitleBar.svelte";
import { settings } from "./settings.svelte.js";

let app: ReturnType<typeof mount> | null = null;

afterEach(() => {
  if (app !== null) unmount(app);
  app = null;
  document.body.replaceChildren();
  settings.skipAllApprovals = false;
  settings.open = false;
});

function render(): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  app = mount(TitleBar, { target, props: { pickDirectory: async () => null } });
  flushSync();
  return target;
}

describe("the approvals-off chip", () => {
  it("is absent while approvals ask", () => {
    expect(render().querySelector(".approvals-off")).toBeNull();
  });

  it("shows while they are off and opens the policy setting", () => {
    settings.skipAllApprovals = true;
    const target = render();
    const chip = target.querySelector<HTMLButtonElement>(".approvals-off");
    expect(chip?.textContent).toBe("Approvals are off");
    chip!.click();
    flushSync();
    expect(settings.open).toBe(true);
    expect(settings.section).toBe("policy");
  });
});
