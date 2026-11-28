// @vitest-environment jsdom
//
// SETREST-01: the packaged app never granted the opener plugin's open_url
// permission, so every external link did nothing, silently. The windows that
// render links now grant it for web links (and the macOS settings deep links
// computer use opens), and a refused or failed open says so.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const opener = vi.hoisted(() => ({ openUrl: vi.fn() }));
vi.mock("@tauri-apps/plugin-opener", () => opener);

import { openExternal } from "./external-link";
import { clearNotifications, toasts } from "./notifications.svelte.js";

function capability(name: string): { windows: string[]; permissions: unknown[] } {
  const path = resolve(process.cwd(), "../shell/capabilities", `${name}.json`);
  return JSON.parse(readFileSync(path, "utf-8"));
}

function openUrlScope(name: string): string[] | null {
  for (const entry of capability(name).permissions) {
    if (typeof entry === "object" && entry !== null && (entry as { identifier?: string }).identifier === "opener:allow-open-url") {
      return ((entry as { allow: Array<{ url: string }> }).allow ?? []).map((a) => a.url);
    }
  }
  return null;
}

describe("the shell grants open_url where links render", () => {
  it.each(["default", "session-windows"])("%s", (name) => {
    const scope = openUrlScope(name);
    expect(scope).not.toBeNull();
    expect(scope).toEqual(expect.arrayContaining(["https://*", "http://*", "x-apple.systemsettings:*"]));
    // No catch-all: a file: or custom-scheme URL from a reply is not opened.
    expect(scope).not.toContain("*");
    expect(scope!.some((url) => url.startsWith("file:"))).toBe(false);
  });

  it("the main and session windows are the ones covered", () => {
    expect(capability("default").windows).toEqual(expect.arrayContaining(["main", "session-*"]));
  });
});

describe("openExternal", () => {
  beforeEach(() => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    clearNotifications();
    opener.openUrl.mockReset();
  });
  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });

  it("opens through the plugin", async () => {
    opener.openUrl.mockResolvedValue(undefined);
    expect(await openExternal("https://example.com/docs")).toBe(true);
    expect(opener.openUrl).toHaveBeenCalledWith("https://example.com/docs");
    expect(toasts).toHaveLength(0);
  });

  it("a refused open copies the address and says so", async () => {
    opener.openUrl.mockRejectedValue(new Error("opener.open_url not allowed"));
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    expect(await openExternal("https://example.com/releases")).toBe(false);
    expect(writeText).toHaveBeenCalledWith("https://example.com/releases");
    expect(toasts.map((t) => t.title)).toEqual(["Couldn't open the link"]);
    expect(toasts[0].body).toContain("on your clipboard");
  });
});
