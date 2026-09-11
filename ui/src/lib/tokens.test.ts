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
import { readFileSync, readdirSync } from "node:fs";
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

// ── Text contrast ─────────────────────────────────────────────────────────
//
// The three ink steps have to reach WCAG AA (4.5:1) on the three surfaces
// they are actually set on. Muted ink is the one that drifts: it is the
// quietest step, so every "make it a little quieter" nudge lands here, and
// the rail's section labels, placeholders and hints are set in it on the
// sunken wash — the surface with the least headroom.

/** Relative luminance of a #rrggbb colour (WCAG 2.x). */
function luminance(hex: string): number {
	const n = parseInt(hex.slice(1), 16);
	const [r, g, b] = [16, 8, 0].map((shift) => {
		const c = ((n >> shift) & 255) / 255;
		return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
	});
	return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
	const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
	return (hi + 0.05) / (lo + 0.05);
}

describe("text contrast", () => {
	const palettes = {
		light: tokensOf(":root {"),
		dark: tokensOf(':root[data-theme="dark"]'),
	};
	const inks = ["--color-ink", "--color-ink-secondary", "--color-ink-muted"];
	const surfaces = ["--color-ground", "--color-lifted", "--color-sunken"];

	for (const [theme, palette] of Object.entries(palettes)) {
		for (const ink of inks) {
			for (const surface of surfaces) {
				it(`${theme}: ${ink} reaches AA on ${surface}`, () => {
					expect(palette[ink]).toMatch(/^#[0-9a-f]{6}$/i);
					expect(palette[surface]).toMatch(/^#[0-9a-f]{6}$/i);
					expect(contrast(palette[ink], palette[surface])).toBeGreaterThanOrEqual(4.5);
				});
			}
		}
	}
});

// ── One vocabulary ────────────────────────────────────────────────────────
//
// The September 2026 design rounds retired the TD-1001–TD-1404 alias names
// and moved every component onto the canonical set. Two things keep it
// that way. A name nothing declares (--fg-muted, --radius — both were in
// the tree) falls back to the browser default without a word, so every
// var(--…) in the app has to be declared by some stylesheet or component.
// And the retired names must stay retired: not declared, not referenced.

/** Every source file under `dir`, tests excluded. */
function walk(dir: string, out: string[] = []): string[] {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = `${dir}/${entry.name}`;
		if (entry.isDirectory()) walk(path, out);
		else if (/\.(svelte|css|ts)$/.test(entry.name) && !/\.test\.ts$/.test(entry.name)) out.push(path);
	}
	return out;
}

const RETIRED = [
	"--color-bg",
	"--color-bg-subtle",
	"--color-bg-raised",
	"--color-border",
	"--color-text",
	"--color-text-secondary",
	"--color-text-muted",
	"--color-accent-text",
	"--color-success",
	"--color-warning",
	"--color-danger",
	"--color-info",
	"--font-family",
];

describe("one token vocabulary", () => {
	const root = resolve(process.cwd(), "src");
	const files = walk(root);
	const sources = new Map(files.map((file) => [file.slice(root.length + 1), readFileSync(file, "utf-8")]));
	// A declaration is `--name:` in a rule, or `--name=` / `--name:` where a
	// component sets one inline for its children (SplitPane's --divider-hit).
	const declared = new Set<string>();
	for (const source of sources.values()) {
		for (const m of source.matchAll(/(--[\w-]+)\s*[:=]/g)) declared.add(m[1]);
	}

	it("walks the app tree, so an empty walk cannot pass this file", () => {
		expect(files.length).toBeGreaterThan(50);
		expect(declared.has("--color-ink")).toBe(true);
		expect(declared.has("--space-2")).toBe(true);
	});

	it("only references names some stylesheet declares", () => {
		const missing: string[] = [];
		for (const [name, source] of sources) {
			for (const m of source.matchAll(/var\(\s*(--[\w-]+)/g)) {
				if (!declared.has(m[1])) missing.push(`${name}: ${m[1]}`);
			}
		}
		expect(missing).toEqual([]);
	});

	it("has retired the legacy aliases, and nothing reaches for them", () => {
		for (const alias of RETIRED) expect(declared.has(alias)).toBe(false);
		const uses: string[] = [];
		for (const [name, source] of sources) {
			for (const alias of RETIRED) {
				if (source.includes(`var(${alias})`) || source.includes(`var(${alias},`)) {
					uses.push(`${name}: ${alias}`);
				}
			}
		}
		expect(uses).toEqual([]);
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
