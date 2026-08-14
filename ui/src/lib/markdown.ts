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
  configured = true;
}

/** Render markdown to sanitized HTML, safe for {@html}. */
export function renderMarkdown(md: string): string {
  ensureConfigured();
  const html = marked.parse(md, { async: false });
  return DOMPurify.sanitize(html);
}
