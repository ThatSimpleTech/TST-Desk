// Canvas plumbing for the backgrounds (TD-5202): the pixel-ratio caps, the
// backing-store fit, reading colours out of tokens in every syntax the
// stylesheet uses, the theme's blend mode, and noticing a move to a screen
// of another density. Also a guard that no background's maths reaches for
// Math.random.

import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import {
	DPR_CAP,
	WINDOW_DPR_CAP,
	blendMode,
	canvasScale,
	cssColor,
	fitCanvas,
	parseColor,
	tokenColor,
	tokenNumber,
	watchPixelRatio,
} from "./paint";

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

	it("caps a layer behind the whole window lower than a preview tile", () => {
		expect(WINDOW_DPR_CAP).toBe(1.5);
		expect(WINDOW_DPR_CAP).toBeLessThan(DPR_CAP);
		expect(canvasScale(2, 1, WINDOW_DPR_CAP)).toBe(1.5);
		expect(canvasScale(1.25, 1, WINDOW_DPR_CAP)).toBe(1.25);
		expect(canvasScale(3, 0.5, WINDOW_DPR_CAP)).toBe(0.75);
		expect(canvasScale(2, 1, Number.NaN)).toBe(DPR_CAP);
	});
});

/** A window whose matchMedia hands out queries the test can fire. */
function densityWindow(ratio: number) {
	const queries: { media: string; listeners: Set<() => void> }[] = [];
	const win = {
		devicePixelRatio: ratio,
		matchMedia(media: string) {
			const entry = { media, listeners: new Set<() => void>() };
			queries.push(entry);
			return {
				media,
				matches: true,
				addEventListener: (_type: string, run: () => void) => entry.listeners.add(run),
				removeEventListener: (_type: string, run: () => void) => entry.listeners.delete(run),
			};
		},
	};
	const live = () => queries.filter((q) => q.listeners.size > 0);
	const fire = () => {
		for (const q of live()) for (const run of [...q.listeners]) run();
	};
	return { win: win as unknown as Window & { devicePixelRatio: number }, live, fire };
}

describe("watchPixelRatio", () => {
	it("calls back when the ratio changes, then listens for the new one", () => {
		const { win, live, fire } = densityWindow(1);
		let calls = 0;
		const stop = watchPixelRatio(win, () => calls++);
		expect(live().map((q) => q.media)).toEqual(["(resolution: 1dppx)"]);
		win.devicePixelRatio = 2;
		fire();
		expect(calls).toBe(1);
		expect(live().map((q) => q.media)).toEqual(["(resolution: 2dppx)"]);
		win.devicePixelRatio = 1.5;
		fire();
		expect(calls).toBe(2);
		stop();
		expect(live()).toEqual([]);
		fire();
		expect(calls).toBe(2);
	});

	it("does nothing where there is no matchMedia", () => {
		const stop = watchPixelRatio({ devicePixelRatio: 2 } as unknown as Window, () => {
			throw new Error("never");
		});
		expect(() => stop()).not.toThrow();
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
