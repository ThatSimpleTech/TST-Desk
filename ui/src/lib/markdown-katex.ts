// Hydrate KaTeX placeholders that renderMarkdown() left as source (TD-4706).
// Same split as mermaid: marked stays sync and katex stays off the first
// paint so a transcript with no math does not pay for the fonts.

import katex from "katex";
import "katex/dist/katex.min.css";
import { renderFence } from "./markdown";

function fallback(node: Element, tex: string, display: boolean): void {
	node.outerHTML = display ? renderFence(tex, "latex") : `<code>${escapeHtml(tex)}</code>`;
}

function escapeHtml(text: string): string {
	return text
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;");
}

/** Turn `.md-katex-ph` placeholders under *root* into KaTeX HTML. */
export function hydrateKatex(root: HTMLElement): void {
	const nodes = [...root.querySelectorAll(".md-katex-ph:not([data-hydrated])")];
	for (const node of nodes) {
		const tex = node.querySelector(".md-katex-src")?.textContent ?? "";
		const display = node.getAttribute("data-display") === "1";
		try {
			const html = katex.renderToString(tex, {
				displayMode: display,
				throwOnError: true,
				output: "html",
				strict: "ignore",
			});
			const wrap = document.createElement("span");
			wrap.className = `md-katex md-katex-${display ? "display" : "inline"}`;
			wrap.innerHTML = html;
			wrap.setAttribute("data-hydrated", "");
			node.replaceWith(wrap);
		} catch {
			fallback(node, tex, display);
		}
	}
}
