import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { isNarrowViewport, NARROW_VIEWPORT_PX, narrowMediaQuery, shellChrome } from "./layout-mode";

const SHELL = readFileSync(resolve(process.cwd(), "src/lib/components/AppShell.svelte"), "utf-8");

describe("narrow viewport (TD-3701)", () => {
  it("treats width below 640 as one-pane", () => {
    expect(NARROW_VIEWPORT_PX).toBe(640);
    expect(isNarrowViewport(639)).toBe(true);
    expect(isNarrowViewport(640)).toBe(false);
    expect(narrowMediaQuery()).toBe("(max-width: 639px)");
  });

  it("hides rail and inspector by default when narrow; both stay optional", () => {
    expect(shellChrome(375)).toEqual({ rail: false, inspector: false });
    expect(shellChrome(375, { showRail: true })).toEqual({ rail: true, inspector: false });
    expect(shellChrome(375, { showInspector: true })).toEqual({ rail: false, inspector: true });
    expect(shellChrome(1024, { showRail: false, showInspector: false })).toEqual({
      rail: true,
      inspector: true,
    });
  });

  it("AppShell CSS uses the same breakpoint the helper names", () => {
    expect(SHELL).toContain(`@media ${narrowMediaQuery()}`);
    expect(SHELL).toContain("shell-show-rail");
    expect(SHELL).toContain("shell-show-inspector");
  });
});
