// @vitest-environment jsdom
//
// TD-5150: the Activity panel (and the phone, which runs the same app)
// draws an in-turn tool-output trim as its own muted row, and a real
// compaction as before. Mounted from the daemon's own fixtures.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import TimelineEntryRow from "./components/TimelineEntryRow.svelte";
import { eventToEntry } from "./timeline";
import type { ContextCompacted } from "./protocol";
import fixtures from "./protocol-fixtures.json";

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

beforeEach(() => {
	target = document.createElement("div");
	document.body.appendChild(target);
});

afterEach(() => {
	if (component !== null) unmount(component);
	component = null;
	target.remove();
});

function row(event: ContextCompacted): HTMLElement {
	const entry = eventToEntry(event);
	if (entry === null) throw new Error("no row");
	component = mount(TimelineEntryRow, { target, props: { entry, expanded: false, ontoggle: () => {} } });
	flushSync();
	const el = target.querySelector<HTMLElement>(".row");
	if (el === null) throw new Error("row not rendered");
	return el;
}

describe("TimelineEntryRow for context_compacted (TD-5150)", () => {
	it("draws a trim as a muted Trim row with the results and tokens freed", () => {
		const el = row(fixtures.context_compacted_inflight_trim as ContextCompacted);
		expect(el.classList.contains("row--quiet")).toBe(true);
		expect(el.classList.contains("row--neutral")).toBe(true);
		expect(el.querySelector(".kind")?.textContent).toBe("Trim");
		expect(el.querySelector(".title")?.textContent).toBe("Trimmed tool output (3 results)");
		expect(el.querySelector(".preview")?.textContent).toBe("1200 tokens freed · 30500 → 29300");
	});

	it("keeps a compaction loud", () => {
		const el = row(fixtures.context_compacted as ContextCompacted);
		expect(el.classList.contains("row--quiet")).toBe(false);
		expect(el.classList.contains("row--warning")).toBe(true);
		expect(el.querySelector(".kind")?.textContent).toBe("Compaction");
		expect(el.querySelector(".title")?.textContent).toBe("Context compacted");
	});
});
