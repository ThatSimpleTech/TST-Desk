// Artifact list + preview helpers (TD-3202).
//
// The rail's Artifacts surface is a flat list of the bound session's
// products — not the Files pane (writes this session) and not a tree.
// Preview kind and the Open-in-OS gate are decided here so the store
// and the pane share one rule. Location is not on the wire (TD-3201);
// session-persist bytes are stored at `artifacts/{id}`.

export type ArtifactPreviewKind = "markdown" | "html" | "code";

/** Empty `sandbox` is the locked-down iframe: no scripts, no same-origin,
 *  no popups, no forms. Any `allow-*` token here is a defect. */
export const HTML_PREVIEW_SANDBOX = "";

/** Blocks network fetches from preview HTML (`<img src=https://…>` etc.). */
export const HTML_PREVIEW_CSP =
	"default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; " +
	"base-uri 'none'; form-action 'none'; frame-src 'none'; object-src 'none'; " +
	"connect-src 'none'; media-src 'none'";

export function artifactsEmptyCopy(hasSession: boolean): string {
	if (!hasSession) return "Bind a session to see its artifacts.";
	return "Artifacts appear here as this session produces them.";
}

/** Session-data-dir bytes land at `artifacts/{artifact_id}` (TD-3201). */
export function isWorkspaceArtifact(path: string, artifactId: string): boolean {
	return posixPath(path) !== `artifacts/${artifactId}`;
}

/** Absolute path to hand to `open_path`, or null when it is not a workspace file. */
export function workspaceOpenPath(
	workspacePath: string | null,
	artifactPath: string,
	artifactId: string,
): string | null {
	if (workspacePath === null || workspacePath === "") return null;
	if (!isWorkspaceArtifact(artifactPath, artifactId)) return null;
	return joinUnderRoot(workspacePath, artifactPath);
}

/** Join a relative path under root. Refuses absolute, drive, and `..` segments. */
export function joinUnderRoot(root: string, rel: string): string | null {
	const normalized = posixPath(rel);
	if (normalized === "" || normalized === ".") return null;
	if (normalized.startsWith("/") || /^[a-zA-Z]:/.test(normalized)) return null;
	if (normalized.split("/").some((part) => part === ".." || part === "")) return null;
	const trimmed = root.replace(/[/\\]+$/, "");
	if (trimmed === "") return null;
	return `${trimmed}/${normalized}`;
}

export function artifactPreviewKind(mime: string, path: string): ArtifactPreviewKind {
	const m = mime.toLowerCase();
	const p = posixPath(path).toLowerCase();
	if (m === "text/html" || m === "application/xhtml+xml" || p.endsWith(".html") || p.endsWith(".htm")) {
		return "html";
	}
	if (m === "text/markdown" || m === "text/x-markdown" || p.endsWith(".md") || p.endsWith(".markdown")) {
		return "markdown";
	}
	return "code";
}

/** Highlighted-code preview reuses the chat markdown pipeline (no Monaco). */
export function asFencedMarkdown(source: string, path: string): string {
	const lang = fenceLanguage(path);
	let ticks = "```";
	while (source.includes(ticks)) ticks += "`";
	return `${ticks}${lang}\n${source}\n${ticks}`;
}

export function fenceLanguage(path: string): string {
	const ext = posixPath(path).split(".").pop()?.toLowerCase() ?? "";
	const map: Record<string, string> = {
		ts: "typescript",
		tsx: "typescript",
		js: "javascript",
		jsx: "javascript",
		py: "python",
		rs: "rust",
		go: "go",
		json: "json",
		css: "css",
		scss: "scss",
		sh: "bash",
		bash: "bash",
		zsh: "bash",
		toml: "toml",
		yaml: "yaml",
		yml: "yaml",
		html: "xml",
		htm: "xml",
		svelte: "xml",
		c: "c",
		h: "c",
		cpp: "cpp",
		java: "java",
		rb: "ruby",
		sql: "sql",
		txt: "plaintext",
	};
	return map[ext] ?? "";
}

/** `srcdoc` with a first-wins CSP so preview HTML cannot reach the network. */
export function htmlPreviewSrcdoc(html: string): string {
	return (
		`<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy" content="${HTML_PREVIEW_CSP}"></head>` +
		`<body>${html}</body></html>`
	);
}

export async function copyArtifactSource(
	source: string,
	write: (text: string) => Promise<void> = writeClipboard,
): Promise<boolean> {
	try {
		await write(source);
		return true;
	} catch {
		return false;
	}
}

export async function openArtifactInEditor(
	workspacePath: string | null,
	artifactPath: string,
	artifactId: string,
	open: (path: string) => Promise<boolean> = invokeOpenPath,
): Promise<boolean> {
	const resolved = workspaceOpenPath(workspacePath, artifactPath, artifactId);
	if (resolved === null) return false;
	return open(resolved);
}

function posixPath(path: string): string {
	return path.replace(/\\/g, "/");
}

async function writeClipboard(text: string): Promise<void> {
	await navigator.clipboard.writeText(text);
}

async function invokeOpenPath(path: string): Promise<boolean> {
	try {
		const { invoke } = await import("@tauri-apps/api/core");
		await invoke("open_path", { path });
		return true;
	} catch {
		return false;
	}
}
