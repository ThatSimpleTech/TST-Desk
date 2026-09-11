// Markdown rendering pipeline (TD-1004, mermaid/KaTeX TD-4706).
//
// Assistant output is model-generated content rendered as HTML, so
// sanitization is mandatory, not optional: marked parses, highlight.js
// highlights fenced blocks, DOMPurify strips anything dangerous. Code blocks
// carry a copy button wired by delegation in Markdown.svelte.
//
// Mermaid and math fences emit placeholders; Markdown.svelte hydrates
// SVG / KaTeX off the marked path so a failed parse can fall back to the
// original fence, and so a transcript with neither does not pay for the
// libraries on first paint.
//
// Pure module, no Svelte imports. Tests run under jsdom (DOMPurify needs
// a window, and happy-dom is broken + unsupported by DOMPurify >= 3.4.8);
// the app runs client-side only (ssr = false).

import { marked } from "marked";
import DOMPurify from "dompurify";
import hljs from "highlight.js";

const MATH_FENCE_LANGS = new Set(["math", "katex", "latex"]);

let configured = false;

function escapeHtml(text: string): string {
	return text
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;");
}

/** Highlighted fence used for ordinary code and for mermaid/KaTeX fallbacks. */
export function renderFence(text: string, lang?: string): string {
	const language =
		lang !== undefined && lang !== "" && hljs.getLanguage(lang) ? lang : "plaintext";
	const highlighted = hljs.highlight(text, { language }).value;
	// A span, not a button: `button` sits on the sanitizer forbid
	// list (TD-4807) so model output can never render one.
	return `<div class="code-block"><span class="copy-btn" data-copy-btn>Copy</span><pre><code class="hljs language-${language}">${highlighted}</code></pre></div>`;
}

function katexPlaceholder(tex: string, displayMode: boolean): string {
	const kind = displayMode ? "display" : "inline";
	return (
		`<span class="md-katex-ph md-katex-${kind}" data-display="${displayMode ? "1" : "0"}">` +
		`<span class="md-katex-src">${escapeHtml(tex)}</span></span>`
	);
}

function mermaidPlaceholder(text: string): string {
	return `<div class="md-mermaid"><pre class="md-mermaid-src">${escapeHtml(text)}</pre></div>`;
}

function ensureConfigured(): void {
	if (configured) return;
	marked.use({
		renderer: {
			code({ text, lang }: { text: string; lang?: string }): string {
				const language = (lang ?? "").trim().toLowerCase();
				if (language === "mermaid") {
					return mermaidPlaceholder(text);
				}
				if (MATH_FENCE_LANGS.has(language)) {
					return katexPlaceholder(text, true);
				}
				return renderFence(text, lang);
			},
			// Images wait for TD-4705: capability-gated vision attach, not a
			// markdown <img>. Keep the alt as inert text so a secret in the
			// URL never becomes a fetch.
			image({ text }: { href: string; text: string }): string {
				const alt = text.trim() === "" ? "image" : text.trim();
				return `[${escapeHtml(alt)}]`;
			},
		},
		extensions: [
			{
				name: "katexDisplay",
				level: "block",
				start(src: string): number | undefined {
					const i = src.indexOf("$$");
					return i === -1 ? undefined : i;
				},
				tokenizer(src: string): { type: string; raw: string; text: string } | undefined {
					const match = /^\$\$([\s\S]+?)\$\$/.exec(src);
					if (match === null) return undefined;
					return { type: "katexDisplay", raw: match[0], text: match[1].trim() };
				},
				renderer(token): string {
					return katexPlaceholder(typeof token.text === "string" ? token.text : "", true);
				},
			},
			{
				name: "katexInline",
				level: "inline",
				start(src: string): number | undefined {
					const i = src.indexOf("\\(");
					return i === -1 ? undefined : i;
				},
				tokenizer(src: string): { type: string; raw: string; text: string } | undefined {
					const match = /^\\\(([\s\S]+?)\\\)/.exec(src);
					if (match === null) return undefined;
					return { type: "katexInline", raw: match[0], text: match[1].trim() };
				},
				renderer(token): string {
					return katexPlaceholder(typeof token.text === "string" ? token.text : "", false);
				},
			},
		],
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

// Interactive form chrome is forbidden outright (TD-4807): assistant text
// renders next to real approval cards, and DOMPurify's default profile
// allows form elements — a lookalike approval form is a phishing surface
// inside the trust boundary. Stripped tags leave their text behind, which
// is inert.
const FORBIDDEN_TAGS = ["form", "input", "textarea", "select", "option", "optgroup", "button"];

/** Render markdown to sanitized HTML, safe for {@html}. */
export function renderMarkdown(md: string): string {
	ensureConfigured();
	const html = marked.parse(md, { async: false });
	return DOMPurify.sanitize(html, { FORBID_TAGS: FORBIDDEN_TAGS });
}
