// Project-home surface store (TD-2801).
//
// The rail's Home / Projects / Artifacts / Scheduled rows are honest
// about which surface the window is showing. Selecting a project is a
// UI choice — it does not attach a session. New chat and a recent
// click do that.

import type { RailSurface } from "./rail";

export const projects = $state({
	/** The main pane: chat (home), the project list/home, artifacts, or scheduled. */
	surface: "home" as RailSurface,
	/** Workspace path of the open project home; null is the list. */
	selectedPath: null as string | null,
});

export function showHome(): void {
	projects.surface = "home";
}

/** Open the project list. Clears any selected home so Projects is the list. */
export function showProjects(): void {
	projects.surface = "projects";
	projects.selectedPath = null;
}

/** Open the bound session's artifact list (TD-3202). */
export function showArtifacts(): void {
	projects.surface = "artifacts";
}

/** Open the scheduled-job list (TD-3805). */
export function showScheduled(): void {
	projects.surface = "scheduled";
}

/** Open one project's home. */
export function selectProject(path: string): void {
	projects.selectedPath = path;
	projects.surface = "projects";
}

export function resetProjects(): void {
	projects.surface = "home";
	projects.selectedPath = null;
}
