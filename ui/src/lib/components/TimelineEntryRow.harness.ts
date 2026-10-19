// Test-only mount surface for e2e/trim-row.spec.ts (TD-5150). jsdom has no
// layout, so whether a collapsed row still shows its numbers at a given
// pane width is measured in a real browser: the spec serves a blank page
// from the walk's dev server, loads this module and calls
// `window.__rowHarness.show`. Nothing in the app imports it.

import "../../app.css";
import { flushSync, mount, unmount } from "svelte";
import TimelineEntryRow from "./TimelineEntryRow.svelte";
import { eventToEntry } from "../timeline";
import type { DaemonEventUnion } from "../protocol";
import fixtures from "../protocol-fixtures.json";

/** What a reader sees of each part of the collapsed row: the text up to
 *  where the ellipsis cuts it, or "" when the part has no width at all. */
export interface RowReading {
	kind: string;
	title: string;
	preview: string;
}

export interface RowHarness {
	show(fixture: string, paneWidth: number): RowReading;
}

declare global {
	interface Window {
		__rowHarness?: RowHarness;
	}
}

/** How much of a single-line, ellipsis-cut span is on screen. */
function visiblePart(span: HTMLElement | null): string {
	if (span === null) return "";
	const text = span.textContent ?? "";
	if (span.clientWidth === 0 || text === "") return "";
	if (span.scrollWidth <= span.clientWidth) return text;
	const node = span.firstChild;
	if (node === null || node.nodeType !== Node.TEXT_NODE) return "";
	// The ellipsis is drawn in the span's own font at its right edge.
	const probe = document.createElement("span");
	probe.textContent = "…";
	probe.style.font = getComputedStyle(span).font;
	probe.style.position = "absolute";
	probe.style.visibility = "hidden";
	document.body.appendChild(probe);
	const ellipsis = probe.getBoundingClientRect().width;
	probe.remove();
	const edge = span.getBoundingClientRect().left + span.clientWidth - ellipsis + 0.5;
	const range = document.createRange();
	let lo = 0;
	let hi = text.length;
	while (lo < hi) {
		const mid = Math.ceil((lo + hi) / 2);
		range.setStart(node, 0);
		range.setEnd(node, mid);
		if (range.getBoundingClientRect().right <= edge) lo = mid;
		else hi = mid - 1;
	}
	return text.slice(0, lo);
}

let current: ReturnType<typeof mount> | null = null;

window.__rowHarness = {
	show(fixture, paneWidth) {
		const event = (fixtures as Record<string, unknown>)[fixture] as DaemonEventUnion | undefined;
		if (event === undefined) throw new Error(`no fixture ${fixture}`);
		const entry = eventToEntry(event);
		if (entry === null) throw new Error(`${fixture} has no timeline row`);
		if (current !== null) unmount(current);
		const pane = document.getElementById("pane");
		if (pane === null) throw new Error("no #pane");
		pane.style.width = `${paneWidth}px`;
		current = mount(TimelineEntryRow, { target: pane, props: { entry, expanded: false, ontoggle: () => {} } });
		flushSync();
		return {
			kind: visiblePart(pane.querySelector<HTMLElement>(".kind")),
			title: visiblePart(pane.querySelector<HTMLElement>(".title")),
			preview: visiblePart(pane.querySelector<HTMLElement>(".preview")),
		};
	},
};
