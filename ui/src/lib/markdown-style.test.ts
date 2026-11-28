// @vitest-environment jsdom
//
// CHAT-01: nothing in a chat message may style the app. A <style> block or a
// style attribute in a reply or a pasted message used to restyle the whole
// window (blank it on every launch, or lay a fake prompt over everything).

import { describe, expect, it, vi } from "vitest";
import { FORBIDDEN_ATTRS, FORBIDDEN_TAGS, renderMarkdown } from "./markdown";
import { MERMAID_SECURE, ensureStarted } from "./markdown-mermaid";

describe("chat HTML carries no styling", () => {
  it.each([
    "<style>body{display:none}</style>",
    "before <style>body::after{content:'Sign in';position:fixed;inset:0;z-index:2147483647}</style> after",
    "<svg><style>*{display:none}</style></svg>",
    '<div style="position:fixed;inset:0;background:#fff">Approve</div>',
    '<span style="display:none">hidden</span> visible',
    '<link rel="stylesheet" href="https://example.com/x.css">',
    '<base href="https://example.com/">',
    '<meta http-equiv="refresh" content="0;url=https://example.com">',
  ])("drops it from %s", (md) => {
    const html = renderMarkdown(md);
    expect(html).not.toMatch(/<style|style=|<link|<base|<meta/i);
  });

  it("keeps the text of a stripped element", () => {
    expect(renderMarkdown('<span style="color:red">kept</span>')).toContain("kept");
  });

  it("leaves a style tag in a code fence as text", () => {
    const html = renderMarkdown("```html\n<style>body{display:none}</style>\n```");
    expect(html).not.toMatch(/<style/);
    expect(html).toContain("&lt;");
  });

  it("names the forbidden set", () => {
    expect(FORBIDDEN_TAGS).toEqual(expect.arrayContaining(["style", "link", "base", "meta"]));
    expect(FORBIDDEN_ATTRS).toEqual(["style"]);
  });
});

describe("a diagram's directives cannot set its CSS", () => {
  it("locks the theme keys", () => {
    const initialize = vi.fn();
    ensureStarted({ initialize });
    const config = initialize.mock.calls[0][0] as { secure: string[]; securityLevel: string };
    expect(config.securityLevel).toBe("strict");
    expect(config.secure).toEqual(MERMAID_SECURE);
    expect(config.secure).toEqual(expect.arrayContaining(["themeCSS", "themeVariables", "fontFamily"]));
  });
});
