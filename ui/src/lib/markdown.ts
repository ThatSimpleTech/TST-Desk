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
		const cls = node.getAttribute("class");
		if (cls !== null) {
			const own = cls.split(/\s+/).filter((c) => OWN_CLASS.test(c));
			if (own.length > 0) node.setAttribute("class", own.join(" "));
			else node.removeAttribute("class");
		}
		// href is allowed on <a> only; anything else that kept one loses it.
		if (!(node instanceof HTMLAnchorElement)) {
			node.removeAttribute("href");
			return;
		}
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

// An allow-list, not a forbid-list (item 5: CHAT-01, RENDERSEC-01, -03, -04,
// -05). Chat HTML sits in the app's own document next to real approval
// cards, so anything not named here is dropped, its text kept as inert text:
//
// - no style, <style>, <link>, <base> or <meta>: a message must not restyle
//   or re-base the window (CHAT-01);
// - no SVG and no image maps: an SVG <a> or an <area> navigated the whole
//   webview past the link guard (RENDERSEC-01);
// - no id or name: an element named __TAURI_INTERNALS__ clobbered host
//   detection on the phone client (RENDERSEC-03);
// - no role or aria-*: a reply could announce itself to a screen reader as an
//   assertive modal "TST Desk Security" dialog (RENDERSEC-04);
// - no src, srcset, poster, background, <img>, media or <picture>: nothing in a
//   message makes a request (RENDERSEC-05);
// - no form controls (TD-4807).
//
// The CSP stays as the second wall, not the first.
export const ALLOWED_TAGS = [
	"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
	"strong", "em", "b", "i", "u", "s", "del", "ins", "mark", "small", "sub", "sup",
	"code", "pre", "kbd", "samp", "var", "span", "div", "blockquote", "q", "cite", "abbr",
	"ul", "ol", "li", "dl", "dt", "dd",
	"a",
	"table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
	"details", "summary",
];
export const ALLOWED_ATTRS = ["href", "title", "class", "align", "start", "open", "data-copy-btn", "data-display"];

// The classes the renderer itself writes: code blocks and their copy chip,
// highlight.js tokens, and the math and diagram placeholders drawn after
// this pass. A class a message names itself must not borrow app styles.
const OWN_CLASS = /^(?:code-block|copy-btn|hljs(?:-[a-z0-9_-]+)?|language-[a-z0-9_+#-]+|md-katex-[a-z-]+|md-mermaid(?:-src)?)$/;

/** Kept for callers that list what is never allowed (TD-4807). */
export const FORBIDDEN_TAGS = ["form", "input", "textarea", "select", "option", "optgroup", "button", "style", "link", "base", "meta"];
export const FORBIDDEN_ATTRS = ["style"];

/** Render markdown to sanitized HTML, safe for {@html}. */
export function renderMarkdown(md: string): string {
	ensureConfigured();
	const html = marked.parse(md, { async: false });
	return DOMPurify.sanitize(html, {
		ALLOWED_TAGS,
		ALLOWED_ATTR: ALLOWED_ATTRS,
		ALLOW_DATA_ATTR: false,
		ALLOW_ARIA_ATTR: false,
		FORBID_TAGS: FORBIDDEN_TAGS,
		FORBID_ATTR: FORBIDDEN_ATTRS,
		SANITIZE_NAMED_PROPS: true,
	});
}
