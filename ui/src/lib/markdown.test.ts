// @vitest-environment jsdom
//
// Markdown pipeline tests (TD-1004). jsdom supplies the window DOMPurify
// needs; the app itself always has one (ssr = false). jsdom, not happy-dom:
// happy-dom's Node.prototype.nodeName getter breaks DOMPurify >= 3.4.8's
// anti-clobbering (cure53/DOMPurify#1496) and is unsupported upstream.

import { describe, it, expect } from "vitest";
import { renderMarkdown } from "./markdown";

describe("renderMarkdown", () => {
  it("syntax-highlights fenced code blocks", () => {
    const html = renderMarkdown("```python\nprint('hi')\n```");
    expect(html).toContain('class="hljs language-python"');
    // hljs tokenizes keywords into spans — highlighting actually happened.
    expect(html).toMatch(/hljs-[a-z_]+/);
  });

  it("falls back to plaintext for unknown languages", () => {
    const html = renderMarkdown("```nolang\nx\n```");
    expect(html).toContain("language-plaintext");
  });

  it("wraps code blocks with a copy button", () => {
    const html = renderMarkdown("```\nplain\n```");
    expect(html).toContain('class="code-block"');
    expect(html).toContain("data-copy-btn");
  });

  it("strips script tags", () => {
    const html = renderMarkdown("<script>alert(1)</script>\n\nhello");
    expect(html).not.toContain("<script");
    expect(html).toContain("hello");
  });

  it("strips inline event handlers", () => {
    const html = renderMarkdown('<img src="x" onerror="alert(1)">');
    expect(html).not.toContain("onerror");
  });

  it("renders inline code", () => {
    expect(renderMarkdown("run `npm test` now")).toContain("<code>npm test</code>");
  });

  it("renders headings and lists", () => {
    const html = renderMarkdown("# Title\n\n- a\n- b");
    expect(html).toContain("<h1>Title</h1>");
    expect(html).toContain("<li>a</li>");
  });

  it("rewrites http(s) links for external opening (TD-4806)", () => {
    const html = renderMarkdown("[docs](https://example.com)");
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noreferrer noopener"');
  });

  it.each([
    ["javascript:alert(1)"],
    ["data:text/html,<script>alert(1)</script>"],
    ["file:///etc/passwd"],
    ["mailto:a@b.c"],
  ])("drops the non-http(s) link %s (TD-4806)", (href) => {
    const html = renderMarkdown(`[click](${href})`);
    expect(html).not.toContain("href");
    expect(html).toContain("click"); // the text survives; the link is inert
  });

  it("drops relative hrefs — meaningless inside the app (TD-4806)", () => {
    const html = renderMarkdown("[notes](./notes.md)");
    expect(html).not.toContain("href");
    expect(html).toContain("notes");
  });
});
