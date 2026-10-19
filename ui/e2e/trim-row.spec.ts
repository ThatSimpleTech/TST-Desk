import { expect, test } from "@playwright/test";
import type { RowReading } from "../src/lib/components/TimelineEntryRow.harness";

// TD-5150: an in-turn tool-output trim shows as its own quiet row with the
// results trimmed and the tokens freed, and those numbers must be on screen
// without expanding the row. jsdom has no layout, so this measures the real
// row in Chromium at the activity pane's widths: 200px is the narrowest
// pane (MIN_RIGHT_PX), which is what a phone gets; 320px is the desktop
// default (DEFAULT_RIGHT_PX) and 305px is that minus a classic scrollbar.

const PAGE = "/__td5150-trim-row";

test.beforeEach(async ({ page }) => {
	// A blank page on the dev server's origin, so the harness module (and
	// the row's scoped CSS) comes from Vite exactly as the app's does.
	await page.route(`**${PAGE}`, (route) =>
		route.fulfill({
			contentType: "text/html",
			body:
				'<!doctype html><html><head><meta charset="utf-8"></head><body>' +
				'<div id="pane"></div>' +
				'<script type="module" src="/src/lib/components/TimelineEntryRow.harness.ts"></script>' +
				"</body></html>",
		}),
	);
	await page.goto(PAGE);
	await page.waitForFunction(() => window.__rowHarness !== undefined);
});

async function show(page: import("@playwright/test").Page, fixture: string, width: number): Promise<RowReading> {
	return page.evaluate(([f, w]) => window.__rowHarness!.show(f, w), [fixture, width] as const);
}

test("a trim row shows its count and tokens freed on the phone's 200px pane", async ({ page }) => {
	const row = await show(page, "context_compacted_inflight_trim", 200);
	expect(row.kind).toBe("Trim");
	// The fixture trimmed 3 results and freed 30500 - 29300 tokens.
	expect(row.title).toMatch(/^3 results · 1\.2k/);
});

for (const width of [305, 320]) {
	test(`a trim row shows its whole title at the desktop's ${width}px pane`, async ({ page }) => {
		const row = await show(page, "context_compacted_inflight_trim", width);
		expect(row.kind).toBe("Trim");
		expect(row.title).toBe("3 results · 1.2k tokens freed");
		// No "Trimm…" stub beside it.
		expect(row.preview).toBe("");
	});
}

test("a wider pane adds what the trim was", async ({ page }) => {
	const row = await show(page, "context_compacted_inflight_trim", 480);
	expect(row.title).toBe("3 results · 1.2k tokens freed");
	expect(row.preview).toMatch(/^Trimmed tool output · 30500/);
});

test("a compaction row is laid out as before", async ({ page }) => {
	const row = await show(page, "context_compacted", 320);
	expect(row.kind).toBe("Compaction");
	// Its mono title keeps the 40% cap it always had.
	expect(row.title).toMatch(/^Context compact/);
});
