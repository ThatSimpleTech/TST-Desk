// Artifacts rail store (TD-3202).
//
// Bound-session list from `artifact_list` / `artifact_ready`. Click sends
// `open_artifact`; preview bytes come from a wall-limited reader, never
// `fetch()` of an arbitrary URL. Protocol path metadata is daemon truth;
// the UI does not invent a file tree.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { artifactPreviewKind, isWorkspaceArtifact, type ArtifactPreviewKind } from "./artifacts";
import { isTauri } from "./open-file";
import type { Artifact, ArtifactEntry, DaemonEventUnion } from "./protocol";

export interface ArtifactPreview {
	artifactId: string;
	title: string;
	mime: string;
	path: string;
	kind: ArtifactPreviewKind;
	source: string | null;
}

export const artifacts = $state({
	sessionId: null as string | null,
	workspacePath: null as string | null,
	items: [] as ArtifactEntry[],
	selectedId: null as string | null,
	preview: null as ArtifactPreview | null,
	loading: false,
	error: null as string | null,
});

export type ArtifactSourceReader = (args: {
	path: string;
	workspacePath: string | null;
	sessionId: string;
	artifactId: string;
}) => Promise<string>;

let sourceReader: ArtifactSourceReader = defaultSourceReader;
let started = false;
let previewToken = 0;
let stopEvents: (() => void) | null = null;

function ensureStarted(): void {
	if (started) return;
	started = true;
	stopEvents = onEvent(reduce);
}

export function setArtifactSourceReader(reader: ArtifactSourceReader | null): void {
	sourceReader = reader ?? defaultSourceReader;
}

export function startArtifacts(): () => void {
	ensureStarted();
	return () => {
		started = false;
		stopEvents?.();
		stopEvents = null;
	};
}

export function resetArtifacts(): void {
	started = false;
	stopEvents?.();
	stopEvents = null;
	previewToken += 1;
	artifacts.sessionId = null;
	artifacts.workspacePath = null;
	artifacts.items = [];
	artifacts.selectedId = null;
	artifacts.preview = null;
	artifacts.loading = false;
	artifacts.error = null;
	sourceReader = defaultSourceReader;
}

/** Bind the pane to a session and ask the daemon for its list. */
export function bindArtifacts(sessionId: string | null, workspacePath: string | null): void {
	ensureStarted();
	const same = sessionId === artifacts.sessionId;
	artifacts.sessionId = sessionId;
	artifacts.workspacePath = workspacePath;
	if (!same) {
		previewToken += 1;
		artifacts.items = [];
		artifacts.selectedId = null;
		artifacts.preview = null;
		artifacts.error = null;
		artifacts.loading = false;
	}
	if (sessionId !== null) {
		sendToDaemon({ type: "list_artifacts", session_id: sessionId });
	}
}

export function selectArtifact(artifactId: string): boolean {
	const item = artifacts.items.find((row) => row.artifact_id === artifactId);
	if (item === undefined || artifacts.sessionId === null) return false;
	artifacts.selectedId = artifactId;
	artifacts.preview = null;
	artifacts.error = null;
	artifacts.loading = true;
	return sendToDaemon({
		type: "open_artifact",
		session_id: artifacts.sessionId,
		artifact_id: artifactId,
	});
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "artifact_list") {
		if (event.session_id !== artifacts.sessionId) return;
		artifacts.items = event.artifacts;
		if (
			artifacts.selectedId !== null &&
			!event.artifacts.some((row) => row.artifact_id === artifacts.selectedId)
		) {
			previewToken += 1;
			artifacts.selectedId = null;
			artifacts.preview = null;
			artifacts.loading = false;
		}
		return;
	}
	if (event.type === "artifact_ready") {
		if (event.session_id !== artifacts.sessionId) return;
		upsert({
			artifact_id: event.artifact_id,
			title: event.title,
			mime: event.mime,
			path: event.path,
		});
		return;
	}
	if (event.type === "artifact") {
		if (event.session_id !== artifacts.sessionId) return;
		if (event.artifact_id !== artifacts.selectedId) return;
		beginPreview(event);
		return;
	}
	if (event.type === "error" && event.code === "artifact_not_found") {
		if (event.session_id != null && event.session_id !== artifacts.sessionId) return;
		artifacts.loading = false;
		artifacts.error = event.message;
	}
}

function upsert(entry: ArtifactEntry): void {
	const at = artifacts.items.findIndex((row) => row.artifact_id === entry.artifact_id);
	if (at === -1) {
		artifacts.items = [...artifacts.items, entry];
		return;
	}
	const next = artifacts.items.slice();
	next[at] = entry;
	artifacts.items = next;
}

function beginPreview(event: Artifact): void {
	const kind = artifactPreviewKind(event.mime, event.path);
	artifacts.preview = {
		artifactId: event.artifact_id,
		title: event.title,
		mime: event.mime,
		path: event.path,
		kind,
		source: null,
	};
	const token = ++previewToken;
	const sessionId = artifacts.sessionId;
	if (sessionId === null) {
		artifacts.loading = false;
		artifacts.error = "No session is bound.";
		return;
	}
	void sourceReader({
		path: event.path,
		workspacePath: artifacts.workspacePath,
		sessionId,
		artifactId: event.artifact_id,
	}).then(
		(source) => {
			if (token !== previewToken) return;
			if (artifacts.preview === null || artifacts.preview.artifactId !== event.artifact_id) {
				return;
			}
			artifacts.preview = { ...artifacts.preview, source };
			artifacts.loading = false;
		},
		(err: unknown) => {
			if (token !== previewToken) return;
			artifacts.loading = false;
			artifacts.error = err instanceof Error ? err.message : "Could not read artifact.";
		},
	);
}

async function defaultSourceReader(args: {
	path: string;
	workspacePath: string | null;
	sessionId: string;
	artifactId: string;
}): Promise<string> {
	if (!isTauri()) {
		throw new Error("Artifact preview is available in the desktop app.");
	}
	const { invoke } = await import("@tauri-apps/api/core");
	const workspace = isWorkspaceArtifact(args.path, args.artifactId) ? args.workspacePath : null;
	const session_id = workspace === null ? args.sessionId : null;
	return invoke<string>("read_text_file", {
		path: args.path,
		workspace,
		session_id,
	});
}
