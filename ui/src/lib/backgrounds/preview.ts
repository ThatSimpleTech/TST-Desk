// The Settings preview tiles (TD-5202) draw the real backgrounds in
// miniature, and each moves only while the pointer or keyboard focus is on
// it. The tile grid marks everything inside it with this context so the
// drawing components can tell: a preview follows that rule alone, and does
// not also hold still when the window loses focus as the window's own
// background does. A component works the same in a tile or behind the
// window; only this answer differs.

import { getContext, setContext } from "svelte";

const PREVIEW = Symbol("background-preview");

/** Mark everything this component renders as a preview tile's. */
export function markPreviews(): void {
	setContext(PREVIEW, true);
}

/** Whether the calling component is drawing inside a preview tile. Call it
 *  while the component initialises, as with any Svelte context. */
export function inPreview(): boolean {
	return getContext<boolean | undefined>(PREVIEW) === true;
}
