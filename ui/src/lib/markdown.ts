// Markdown rendering pipeline (TD-1004).
//
// Assistant output is model-generated content rendered as HTML, so
// sanitization is mandatory, not optional: marked parses, highlight.js
// highlights fenced blocks, DOMPurify strips anything dangerous. Code blocks
// carry a copy button wired by delegation in Markdown.svelte.
//
// Pure module, no Svelte imports. Tests run under jsdom (DOMPurify needs
// a window, and happy-dom is broken + unsupported by DOMPurify >= 3.4.8);
// the app runs client-side only (ssr = false).

import { marked } from "marked";
import DOMPurify from "dompurify";
import hljs from "highlight.js";

let configured = false;

function ensureConfigured(): void {
  if (configured) return;
  marked.use({
    renderer: {
      code({ text, lang }: { text: string; lang?: string }): string {
        const language = lang !== undefined && lang !== "" && hljs.getLanguage(lang) ? lang : "plaintext";
        const highlighted = hljs.highlight(text, { language }).value;
        return `<div class="code-block"><button type="button" class="copy-btn" data-copy-btn>Copy</button><pre><code class="hljs language-${language}">${highlighted}</code></pre></div>`;
      },
    },
  });
  // Links in model output must never navigate the app webview (TD-4806).
  // http(s) hrefs get target/rel as defense in depth for any click path
  // that bypasses the delegated handler; every other scheme — and
  // relative hrefs, which are meaningless inside the app — loses its
  // href and renders as inert text. The click itself routes through the
  // Tauri opener in Markdown.svelte.
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (!(node instanceof HTMLAnchorElement)) return;
    const href = node.getAttribute("href") ?? "";
    if (!/^https?:\/\//i.test(href.trim())) {
      node.removeAttribute("href");
      node.removeAttribute("target");
      node.removeAttribute("rel");
      return;
    }
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noreferrer noopener");
  });
  configured = true;
}

/** Render markdown to sanitized HTML, safe for {@html}. */
export function renderMarkdown(md: string): string {
  ensureConfigured();
  const html = marked.parse(md, { async: false });
  return DOMPurify.sanitize(html);
}
