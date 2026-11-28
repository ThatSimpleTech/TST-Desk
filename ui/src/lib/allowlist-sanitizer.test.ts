// @vitest-environment jsdom
//
// Item 5: chat HTML goes through an allow-list (CHAT-01, RENDERSEC-01, -03,
// -04, -05). The CSP stays as the second wall.

import { describe, expect, it } from "vitest";

import { renderMarkdown } from "./markdown";

function html(md: string): string {
  return renderMarkdown(md);
}

describe("the allow-list", () => {
  it("keeps no style anywhere (CHAT-01)", () => {
    const out = html('<style>body{display:none}</style><p style="position:fixed">x</p>');
    expect(out).not.toMatch(/<style|style=/i);
  });

  it("keeps no SVG link and no image map (RENDERSEC-01)", () => {
    const out = html(
      '<svg><a href="https://evil.example/"><text>click</text></a></svg>' +
        '<map name="m"><area href="https://evil.example/" shape="rect" coords="0,0,9,9"></map>',
    );
    expect(out).not.toMatch(/<svg|<area|<map|evil\.example/i);
  });

  it("keeps no id or name (RENDERSEC-03)", () => {
    const out = html('<a id="__TAURI_INTERNALS__" name="x" href="https://example.com/">a</a>');
    expect(out).not.toMatch(/\bid=|\bname=/);
    expect(out).toContain('href="https://example.com/"');
  });

  it("keeps no role or aria (RENDERSEC-04)", () => {
    const out = html(
      '<div role="alertdialog" aria-modal="true" aria-label="TST Desk Security">Approve the next command</div>',
    );
    expect(out).not.toMatch(/role=|aria-/);
    expect(out).toContain("Approve the next command");
  });

  it("keeps nothing that would make a request (RENDERSEC-05)", () => {
    const out = html(
      '<img src="https://x.example/a.png"><picture><source srcset="https://x.example/b.png"></picture>' +
        '<video poster="https://x.example/c.png"></video><audio src="https://x.example/d.mp3"></audio>' +
        '<table background="https://x.example/e.png"><tr><td background="https://x.example/f.png">t</td></tr></table>' +
        '<base href="https://x.example/">',
    );
    expect(out).not.toMatch(/x\.example|<img|<picture|<source|<video|<audio|<base|background=/i);
  });

  it("keeps only the renderer's own classes", () => {
    const fence = html("```js\nconst a = 1;\n```");
    expect(fence).toContain('class="code-block"');
    expect(fence).toMatch(/class="hljs language-js"/);
    const borrowed = html('<div class="approval-card btn btn--accept">Approve</div>');
    expect(borrowed).not.toMatch(/class=/);
  });

  it("still renders markdown: lists, tables, links, code", () => {
    const out = html("# T\n\n- a\n- b\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n[site](https://example.com) `c`");
    for (const tag of ["<h1", "<ul", "<li", "<table", "<td", "<a ", "<code"]) expect(out).toContain(tag);
  });
});
