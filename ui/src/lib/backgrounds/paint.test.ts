// Canvas plumbing for the backgrounds (TD-5202): the pixel-ratio cap, the
// backing-store fit, reading colours out of tokens in every syntax the
// stylesheet uses, and the theme's blend mode. Also a guard that no
// background's maths reaches for Math.random.

import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { DPR_CAP, blendMode, canvasScale, cssColor, fitCanvas, parseColor, tokenColor, tokenNumber } from "./paint";

describe("canvasScale", () => {
	it("caps the device pixel ratio at 2", () => {
		expect(DPR_CAP).toBe(2);
		expect(canvasScale(1)).toBe(1);
		expect(canvasScale(1.5)).toBe(1.5);
		expect(canvasScale(3)).toBe(2);
		expect(canvasScale(4, 0.3)).toBeCloseTo(0.6, 10);
	});

	it("treats a missing or nonsense ratio as 1", () => {
		expect(canvasScale(undefined)).toBe(1);
		expect(canvasScale(0)).toBe(1);
		expect(canvasScale(Number.NaN)).toBe(1);
		expect(canvasScale(2, 0)).toBe(2);
	});
});

describe("fitCanvas", () => {
	it("sizes the backing store and reports a change only when there is one", () => {
		const canvas = { width: 300, height: 150 };
		expect(fitCanvas(canvas, 640, 360, 2)).toBe(true);
		expect(canvas).toEqual({ width: 1280, height: 720 });
		expect(fitCanvas(canvas, 640, 360, 2)).toBe(false);
	});

	it("never sizes a canvas to zero", () => {
		const canvas = { width: 300, height: 150 };
		fitCanvas(canvas, 0, 0, 2);
		expect(canvas).toEqual({ width: 1, height: 1 });
	});
});

describe("parseColor", () => {
	it("reads hex in three, six and eight digits", () => {
		expect(parseColor("#fff")).toEqual({ rgb: [255, 255, 255], alpha: 1 });
		expect(parseColor(" #b4532a ")).toEqual({ rgb: [180, 83, 42], alpha: 1 });
		expect(parseColor("#00000080")?.alpha).toBeCloseTo(128 / 255, 6);
	});

	it("reads rgb() and rgba() in comma and space syntax", () => {
		expect(parseColor("rgb(180 83 42 / 0.10)")).toEqual({ rgb: [180, 83, 42], alpha: 0.1 });
		expect(parseColor("rgba(1, 2, 3, 0.5)")).toEqual({ rgb: [1, 2, 3], alpha: 0.5 });
		expect(parseColor("rgb(10 20 30)")).toEqual({ rgb: [10, 20, 30], alpha: 1 });
		expect(parseColor("rgb(0 0 0 / 40%)")?.alpha).toBeCloseTo(0.4, 10);
	});

	it("returns null for anything else", () => {
		for (const junk of ["", "red", "#12", "hsl(10 20% 30%)", "lighter", "var(--x)"]) {
			expect(parseColor(junk)).toBeNull();
		}
	});

	it("turns a broken token into transparent, never black", () => {
		expect(tokenColor(() => "", "--bg-x")).toEqual({ rgb: [0, 0, 0], alpha: 0 });
		expect(tokenColor(() => "#abcdef", "--bg-x").rgb).toEqual([171, 205, 239]);
	});
});

describe("cssColor and tokenNumber", () => {
	it("multiplies a token's own alpha", () => {
		expect(cssColor({ rgb: [10, 20, 30], alpha: 0.5 }, 0.5)).toBe("rgba(10,20,30,0.25)");
		expect(cssColor({ rgb: [10, 20, 30], alpha: 1 })).toBe("rgba(10,20,30,1)");
	});

	it("reads 0–1 numbers and clamps them", () => {
		expect(tokenNumber(() => "0.74", "--o", 1)).toBeCloseTo(0.74, 10);
		expect(tokenNumber(() => "3", "--o", 1)).toBe(1);
		expect(tokenNumber(() => "", "--o", 0.5)).toBe(0.5);
	});
});

describe("blendMode", () => {
	it("accepts the modes a theme may ask for", () => {
		expect(blendMode("lighter")).toBe("lighter");
		expect(blendMode(" source-over ")).toBe("source-over");
	});

	it("falls back to plain painting for anything else", () => {
		expect(blendMode("")).toBe("source-over");
		expect(blendMode("copy")).toBe("source-over");
	});
});

describe("seeded maths", () => {
	it("never calls Math.random in a background module", () => {
		const dir = resolve(process.cwd(), "src/lib/backgrounds");
		const sources = readdirSync(dir, { withFileTypes: true })
			.map((entry) => entry.name)
			.filter((name) => name.endsWith(".ts") && !name.endsWith(".test.ts"));
		expect(sources.length).toBeGreaterThan(8);
		for (const file of sources) {
			expect(readFileSync(`${dir}/${file}`, "utf-8"), file).not.toMatch(/Math\.random/);
		}
	});
});
