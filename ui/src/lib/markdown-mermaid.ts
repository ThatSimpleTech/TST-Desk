// Hydrate mermaid placeholders that renderMarkdown() left as source
// (TD-4706). Kept off the marked path so a parse failure can fall back
// to the original fence, and so markdown unit tests do not load mermaid.

import DOMPurify from "dompurify";
import { renderFence } from "./markdown";

let started = false;

async function mermaidApi(): Promise<typeof import("mermaid").default> {
	const mod = await import("mermaid");
	return mod.default;
}

function ensureStarted(api: { initialize: (c: Record<string, unknown>) => void }): void {
	if (started) return;
	api.initialize({
		startOnLoad: false,
		securityLevel: "strict",
		theme: "base",
	});
	started = true;
}

function fallbackNode(node: Element, source: string): void {
	node.outerHTML = renderFence(source, "mermaid");
}

/** Turn `.md-mermaid` placeholders under *root* into sanitized SVG. */
export async function hydrateMermaid(root: HTMLElement): Promise<void> {
	const nodes = [...root.querySelectorAll(".md-mermaid:not([data-hydrated])")];
	if (nodes.length === 0) return;
	let api: typeof import("mermaid").default;
	try {
		api = await mermaidApi();
	} catch {
		for (const node of nodes) {
			const source = node.querySelector(".md-mermaid-src")?.textContent ?? "";
			fallbackNode(node, source);
		}
		return;
	}
	ensureStarted(api);
	for (const node of nodes) {
		const source = node.querySelector(".md-mermaid-src")?.textContent ?? "";
		try {
			const id = `mermaid-${crypto.randomUUID()}`;
			const { svg } = await api.render(id, source);
			const clean = DOMPurify.sanitize(svg, {
				USE_PROFILES: { svg: true, svgFilters: true },
			});
			if (clean.trim() === "") {
				fallbackNode(node, source);
				continue;
			}
			node.innerHTML = clean;
			node.setAttribute("data-hydrated", "");
		} catch {
			fallbackNode(node, source);
		}
	}
}
