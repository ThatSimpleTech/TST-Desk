// TD-5266: every frame in the window is sandboxed. A frame shows content
// the agent wrote or chose, so it must not be able to run script with the
// window's origin, navigate the window, or open another one. This reads
// the source of every component, so a frame added later fails here until
// it says what it is allowed to do.

import { describe, expect, it } from "vitest";

// Every component's source, as text. Vite reads them; no file API needed.
const SOURCES = import.meta.glob<string>("./**/*.svelte", {
	query: "?raw",
	import: "default",
	eager: true,
});

interface Frame {
	file: string;
	tag: string;
}

function frames(): Frame[] {
	const found: Frame[] = [];
	for (const [file, source] of Object.entries(SOURCES)) {
		for (const match of source.matchAll(/<iframe\b[^>]*>/gs)) {
			found.push({ file, tag: match[0] });
		}
	}
	return found;
}

/** A literal sandbox value, or null when the attribute is an expression. */
function literalSandbox(tag: string): string | null {
	return /\bsandbox="([^"]*)"/.exec(tag)?.[1] ?? null;
}

describe("frames in the window", () => {
	const all = frames();

	it("are found by this test", () => {
		// Two today: the artifact preview and the Grok preview.
		expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
		expect(all.map((f) => f.file).sort()).toEqual([
			"./lib/components/ArtifactPane.svelte",
			"./lib/components/PreviewPane.svelte",
		]);
	});

	it.each(all)("$file sandboxes its frame", ({ tag }) => {
		expect(tag).toMatch(/\bsandbox(=|\b)/);
	});

	it.each(all)("$file does not hand its frame the window", ({ tag }) => {
		const value = literalSandbox(tag);
		if (value === null) return; // an expression: its constant is tested where it lives
		const tokens = value.split(/\s+/).filter(Boolean);
		// Scripts with the same origin can remove the sandbox.
		expect(tokens.includes("allow-scripts") && tokens.includes("allow-same-origin")).toBe(false);
		for (const never of ["allow-top-navigation", "allow-popups", "allow-popups-to-escape-sandbox"]) {
			expect(tokens).not.toContain(never);
		}
	});

	it("the Grok preview's frame allows nothing", () => {
		const preview = all.find((f) => f.file.endsWith("PreviewPane.svelte"));
		expect(preview && literalSandbox(preview.tag)).toBe("");
	});
});
