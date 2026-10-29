import { expect, test } from "@playwright/test";
import type { MascotReading } from "../src/lib/components/Mascot.harness";

// TD-5191: the mascot waddles in from the right in short hops, rocking side
// to side and looking where it is going. At its spot it jumps, and only the
// shackle turns: it unhooks, spins a full turn about its right leg and drops
// back in as the body lands; the body never turns. jsdom has no layout or
// animation clock, so this freezes the real animation in Chromium and
// measures where things are drawn.

const PAGE = "/__td5191-mascot";

// Three screen pixels per art pixel. The body is 24 pixels wide, the shackle
// spans pixels 4–20 above it, and its right leg is centred on pixel 18.5.
const PX = 3;
const BODY_WIDTH = 24 * PX;
const HINGE = 18.5 * PX;

// The entrance starts 120ms in; each hop takes 220ms and the jump 1.1s.
const START = 120;
const HOP = 220;
const JUMP = 1100;

test.beforeEach(async ({ page }) => {
	// A blank page on the dev server's origin, so the harness module (and
	// the mascot's scoped CSS) comes from Vite exactly as the app's does.
	// The stage is far wider than the longest walk, so the walk is capped.
	await page.route(`**${PAGE}`, (route) =>
		route.fulfill({
			contentType: "text/html",
			body:
				'<!doctype html><html><head><meta charset="utf-8"></head><body>' +
				'<div id="stage" style="padding: 80px"></div>' +
				'<script type="module" src="/src/lib/components/Mascot.harness.ts"></script>' +
				"</body></html>",
		}),
	);
	await page.goto(PAGE);
	await page.waitForFunction(() => window.__mascotHarness !== undefined);
});

async function at(page: import("@playwright/test").Page, ms: number): Promise<MascotReading> {
	return page.evaluate((t) => window.__mascotHarness!.at(t), ms);
}

/** Instants along the jump, from its take-off at the end of the walk. */
function jumpAt(walkMs: number, fraction: number): number {
	return START + walkMs + fraction * JUMP;
}

async function resting(page: import("@playwright/test").Page): Promise<MascotReading> {
	const first = await at(page, 0);
	return at(page, jumpAt(first.walkMs, 1) + 400);
}

test("it waddles in from the right in short hops, looking where it's going", async ({ page }) => {
	const rest = await resting(page);
	// The longest walk: 480px, in hops of about a body's width.
	expect(rest.walkMs).toBe(8 * HOP);
	const hops = rest.walkMs / HOP;

	let previousLeft = Infinity;
	for (let k = 0; k < hops; k++) {
		const peak = await at(page, START + k * HOP + HOP / 2);
		expect(peak.body.left, `hop ${k} moves left`).toBeLessThan(previousLeft);
		previousLeft = peak.body.left;
		expect(peak.body.left, `hop ${k} is still on its way`).toBeGreaterThan(rest.body.left);
		expect(peak.body.top, `hop ${k} is off the ground`).toBeLessThan(rest.body.top - 6);
		// One rock each way per pair of hops: left, right, left, right.
		expect(peak.tilt, `hop ${k} rocks`).toBeCloseTo(k % 2 === 0 ? -9 : 9, 0);
		expect(peak.eyes, `hop ${k} looks left`).toBe("left");
	}
	const first = await at(page, START + HOP / 2);
	expect(first.body.left - rest.body.left).toBeGreaterThan(400);

	const arrived = await at(page, START + rest.walkMs);
	expect(arrived.body.left).toBeCloseTo(rest.body.left, 0);
	expect(arrived.tilt).toBeCloseTo(0, 1);
	expect(arrived.eyes).toBe("ahead");
});

test("the body stands straight and never turns through the jump", async ({ page }) => {
	const rest = await resting(page);
	for (let ms = jumpAt(rest.walkMs, 0); ms <= jumpAt(rest.walkMs, 1); ms += 40) {
		const r = await at(page, ms);
		expect(r.body.width, `body width at ${ms}ms`).toBeCloseTo(BODY_WIDTH, 0);
		expect(r.tilt, `lean at ${ms}ms`).toBeCloseTo(0, 1);
	}
});

test("halfway round, the shackle reaches past the body's right edge", async ({ page }) => {
	const rest = await resting(page);
	const { body, shackle } = await at(page, jumpAt(rest.walkMs, 0.4));
	// Mirrored about the right leg: pixels 17–33 instead of 4–20.
	expect(shackle.left - body.left).toBeCloseTo(17 * PX, 0);
	expect(shackle.right - body.left).toBeCloseTo(33 * PX, 0);
	expect(shackle.right).toBeGreaterThan(body.right + 8 * PX);
});

test("the right leg stays on its hinge through the whole turn", async ({ page }) => {
	const rest = await resting(page);
	for (let ms = jumpAt(rest.walkMs, 0.15); ms <= jumpAt(rest.walkMs, 0.65); ms += 24) {
		const { body, shackle } = await at(page, ms);
		const hinge = body.left + HINGE;
		expect(shackle.left, `left edge at ${ms}ms`).toBeLessThanOrEqual(hinge + 0.5);
		expect(shackle.right, `right edge at ${ms}ms`).toBeGreaterThanOrEqual(hinge - 0.5);
	}
});

test("it unhooks and lifts before turning, and is back in place by the landing", async ({ page }) => {
	const rest = await resting(page);
	const lift = (r: MascotReading) => r.body.top - r.shackle.top;
	expect(lift(await at(page, jumpAt(rest.walkMs, 0.15)))).toBeCloseTo(lift(rest) + 3 * PX, 0);

	const landed = await at(page, jumpAt(rest.walkMs, 0.73));
	expect(lift(landed)).toBeCloseTo(lift(rest), 0);
	expect(landed.shackle.left - landed.body.left).toBeCloseTo(rest.shackle.left - rest.body.left, 0);
	expect(landed.shackle.width).toBeCloseTo(16 * PX, 0);
});
