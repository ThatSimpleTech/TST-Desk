// @vitest-environment jsdom
//
// TD-4706: KaTeX hydration turns a placeholder into typeset HTML, and a
// failed parse falls back to the original fence.

import { afterEach, describe, expect, it } from "vitest";
import { renderMarkdown } from "./markdown";
import { hydrateKatex } from "./markdown-katex";

function mount(md: string): HTMLElement {
	const root = document.createElement("div");
	root.innerHTML = renderMarkdown(md);
	document.body.append(root);
	return root;
}

afterEach(() => {
	document.body.replaceChildren();
});

describe("hydrateKatex (TD-4706)", () => {
	it("typesets a math fence", () => {
		const root = mount("```math\nE = mc^2\n```");
		hydrateKatex(root);
		expect(root.querySelector(".katex")).not.toBeNull();
		expect(root.querySelector(".md-katex-ph")).toBeNull();
	});

	it("falls back to the fence when KaTeX cannot parse", () => {
		const root = mount("```math\n\\notARealCommand{\n```");
		hydrateKatex(root);
		expect(root.querySelector(".katex")).toBeNull();
		expect(root.querySelector(".code-block")).not.toBeNull();
		expect(root.textContent).toContain("notARealCommand");
	});
});
