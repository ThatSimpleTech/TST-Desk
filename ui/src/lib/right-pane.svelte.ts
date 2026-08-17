// Right-pane tab selection (TD-1707).
//
// The Activity | Stack choice used to be local state inside AppShell. The
// command palette has to be able to point at the Stack panel, and a command
// can only drive state that something other than the markup owns — so the
// choice moved out here. AppShell still renders it; it no longer holds it.

export type RightPaneTab = "activity" | "stack";

export const rightPane = $state({ tab: "activity" as RightPaneTab });

export function showRightPane(tab: RightPaneTab): void {
	rightPane.tab = tab;
}

/** Reset for tests. */
export function resetRightPane(): void {
	rightPane.tab = "activity";
}
