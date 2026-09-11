// @vitest-environment jsdom
//
// TD-4706: mermaid hydration turns a placeholder into sanitized SVG, and
// a failed parse falls back to the original fence.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hydrateMermaid } from "./markdown-mermaid";

const render = vi.fn();
const initialize = vi.fn();

vi.mock("mermaid", () => ({
	default: {
		initialize: (...args: unknown[]) => initialize(...args),
		render: (...args: unknown[]) => render(...args),
	},
}));

function placeholder(source: string): HTMLElement {
	const root = document.createElement("div");
	root.innerHTML = `<div class="md-mermaid"><pre class="md-mermaid-src">${source}</pre></div>`;
	document.body.append(root);
	return root;
}

beforeEach(() => {
	render.mockReset();
	initialize.mockReset();
});

afterEach(() => {
	document.body.replaceChildren();
});

describe("hydrateMermaid (TD-4706)", () => {
	it("replaces a valid diagram with sanitized SVG", async () => {
		render.mockResolvedValue({
			svg: '<svg xmlns="http://www.w3.org/2000/svg"><text>ok</text></svg>',
		});
		const root = placeholder("flowchart LR\n  A-->B");
		await hydrateMermaid(root);
		expect(root.querySelector("svg")).not.toBeNull();
		expect(root.querySelector(".md-mermaid")?.getAttribute("data-hydrated")).toBe("");
		expect(initialize).toHaveBeenCalledWith(
			expect.objectContaining({ securityLevel: "strict", startOnLoad: false }),
		);
	});

	it("falls back to the fence when mermaid cannot parse", async () => {
		render.mockRejectedValue(new Error("Parse error"));
		const root = placeholder("not a diagram {");
		await hydrateMermaid(root);
		expect(root.querySelector("svg")).toBeNull();
		expect(root.querySelector(".code-block")).not.toBeNull();
		expect(root.textContent).toContain("not a diagram");
	});

	it("strips script from mermaid SVG before insert", async () => {
		render.mockResolvedValue({
			svg: '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><text>x</text></svg>',
		});
		const root = placeholder("flowchart LR\n  A-->B");
		await hydrateMermaid(root);
		expect(root.innerHTML).not.toContain("<script");
		expect(root.querySelector("svg")).not.toBeNull();
	});
});
