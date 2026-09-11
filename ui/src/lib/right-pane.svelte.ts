// Right-pane tab selection (TD-1707).
//
// The Activity | Stack choice used to be local state inside AppShell. The
// command palette has to be able to point at the Stack panel, and a command
// can only drive state that something other than the markup owns — so the
// choice moved out here. AppShell still renders it; it no longer holds it.
//
// Usage (TD-1706) joined for the same reason: the title-bar meter's hover
// panel links to it, and a link in the header cannot reach state the
// right pane's markup owns.

export type RightPaneTab =
	| "activity"
	| "files"
	| "work"
	| "stack"
	| "usage"
	| "screen"
	| "preview"
	| "plan";

export const rightPane = $state({ tab: "activity" as RightPaneTab });

export function showRightPane(tab: RightPaneTab): void {
	rightPane.tab = tab;
}

/** Reset for tests. */
export function resetRightPane(): void {
	rightPane.tab = "activity";
}
