// The two dark palettes must not drift (TD-1703).
//
// A theme the user can force needs the dark tokens reachable from a plain
// selector, and CSS cannot share one declaration block between a media query
// and `:root[data-theme="dark"]`. So the list is written twice, and this is
// what keeps the copies honest: change one value and the run fails naming it.
//
// It also pins the override direction, which is the part that is easy to get
// backwards — an explicit light choice has to beat a dark OS.

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Same shape as timeline-bench.test.ts: the app tree is browser-typed and
// @types/node is deliberately not a dependency, so tests that touch the
// filesystem use the ambient shims in node-test-shims.d.ts.
const CSS = readFileSync(resolve(process.cwd(), "src/lib/tokens.css"), "utf-8");

/** The declarations of the rule opened by `selector`, as token → value. */
function tokensOf(selector: string): Record<string, string> {
	const at = CSS.indexOf(selector);
	if (at < 0) throw new Error(`no rule for ${selector}`);
	const open = CSS.indexOf("{", at);
	const body = CSS.slice(open + 1, CSS.indexOf("\n}", open));
	const out: Record<string, string> = {};
	for (const line of body.split("\n")) {
		const m = /^\s*(--[\w-]+)\s*:\s*(.+?);\s*$/.exec(line);
		if (m) out[m[1]] = m[2].trim();
	}
	return out;
}

describe("dark palette", () => {
	const media = tokensOf(':root:not([data-theme="light"])');
	const forced = tokensOf(':root[data-theme="dark"]');

	it("is not empty, so a broken parse cannot pass this file", () => {
		expect(Object.keys(media).length).toBeGreaterThan(15);
	});

	it("declares the same tokens in both rules", () => {
		expect(Object.keys(forced).sort()).toEqual(Object.keys(media).sort());
	});

	it("declares the same values in both rules", () => {
		expect(forced).toEqual(media);
	});
});

describe("override direction", () => {
	it("exempts a forced light theme from the OS dark preference", () => {
		// Without the :not(), choosing light on a dark OS would keep the dark
		// palette and the setting would look broken.
		expect(CSS).toContain('@media (prefers-color-scheme: dark)');
		expect(CSS).toMatch(/@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme="light"\]\)/);
	});

	it("reaches dark from a plain selector, so a light OS can be overridden", () => {
		// `:root[data-theme="dark"]` (0,2,0) outranks the media query's
		// `:root:not(...)` — :not() adds no specificity of its own beyond its
		// argument — so the explicit choice wins in both directions.
		expect(CSS).toContain(':root[data-theme="dark"] {');
	});
});
