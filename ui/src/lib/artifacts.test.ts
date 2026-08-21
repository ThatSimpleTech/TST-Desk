import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ArtifactEntry, ClientMessageUnion, DaemonEventUnion } from "./protocol";
import {
	HTML_PREVIEW_CSP,
	HTML_PREVIEW_SANDBOX,
	artifactPreviewKind,
	asFencedMarkdown,
	artifactsEmptyCopy,
	copyArtifactSource,
	htmlPreviewSrcdoc,
	isWorkspaceArtifact,
	joinUnderRoot,
	openArtifactInEditor,
	workspaceOpenPath,
} from "./artifacts";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return true;
	},
}));

import {
	artifacts,
	bindArtifacts,
	resetArtifacts,
	selectArtifact,
	setArtifactSourceReader,
	startArtifacts,
} from "./artifacts.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function entry(over: Partial<ArtifactEntry> & Pick<ArtifactEntry, "artifact_id">): ArtifactEntry {
	return {
		title: over.title ?? over.artifact_id,
		mime: over.mime ?? "text/plain",
		path: over.path ?? `${over.artifact_id}.txt`,
		...over,
	};
}

function flush(): Promise<void> {
	return Promise.resolve();
}

beforeEach(() => {
	mocks.sent.length = 0;
	resetArtifacts();
	startArtifacts();
	mocks.sent.length = 0;
});

afterEach(() => {
	resetArtifacts();
});

describe("rail empty copy", () => {
	it("says the list fills as the session produces artifacts", () => {
		expect(artifactsEmptyCopy(true)).toMatch(/session produces them/i);
		expect(artifactsEmptyCopy(false)).toMatch(/bind a session/i);
	});
});

describe("preview kinds", () => {
	it("classifies markdown, html, and code from mime or path", () => {
		expect(artifactPreviewKind("text/markdown", "x.txt")).toBe("markdown");
		expect(artifactPreviewKind("text/plain", "notes.md")).toBe("markdown");
		expect(artifactPreviewKind("text/html", "x.txt")).toBe("html");
		expect(artifactPreviewKind("text/plain", "preview.html")).toBe("html");
		expect(artifactPreviewKind("text/plain", "main.py")).toBe("code");
		expect(artifactPreviewKind("application/json", "data.json")).toBe("code");
	});

	it("wraps code for the existing highlight pipeline, not Monaco", () => {
		expect(asFencedMarkdown("print(1)", "main.py")).toBe("```python\nprint(1)\n```");
		expect(asFencedMarkdown("a\n```\nb", "x.txt")).toBe("````plaintext\na\n```\nb\n````");
	});
});

describe("html sandbox", () => {
	it("uses an empty sandbox — no scripts, no same-origin, no network tokens", () => {
		expect(HTML_PREVIEW_SANDBOX).toBe("");
		expect(HTML_PREVIEW_SANDBOX).not.toMatch(/allow-/);
	});

	it("puts a default-src none CSP first in srcdoc so tags cannot fetch", () => {
		const srcdoc = htmlPreviewSrcdoc('<img src="https://example.com/x.png">');
		expect(srcdoc).toContain(HTML_PREVIEW_CSP);
		expect(srcdoc.indexOf("Content-Security-Policy")).toBeLessThan(srcdoc.indexOf("<img"));
		expect(HTML_PREVIEW_CSP).toContain("default-src 'none'");
		expect(HTML_PREVIEW_CSP).toContain("connect-src 'none'");
		expect(HTML_PREVIEW_CSP).not.toMatch(/allow-scripts|allow-same-origin/);
	});
});

describe("open-in-OS gate", () => {
	it("opens a workspace-relative path and refuses session-persist bytes", () => {
		expect(isWorkspaceArtifact("notes.md", "art1")).toBe(true);
		expect(isWorkspaceArtifact("artifacts/art1", "art1")).toBe(false);
		expect(workspaceOpenPath("/ws", "notes.md", "art1")).toBe("/ws/notes.md");
		expect(workspaceOpenPath("/ws", "artifacts/art1", "art1")).toBeNull();
		expect(workspaceOpenPath(null, "notes.md", "art1")).toBeNull();
	});

	it("refuses path escape when resolving a workspace open", () => {
		expect(joinUnderRoot("/ws", "../etc/passwd")).toBeNull();
		expect(joinUnderRoot("/ws", "/etc/passwd")).toBeNull();
		expect(joinUnderRoot("/ws", "docs/report.md")).toBe("/ws/docs/report.md");
	});

	it("does not call the opener for a session-data-dir artifact", async () => {
		const opened: string[] = [];
		expect(
			await openArtifactInEditor("/ws", "artifacts/a1", "a1", async (path) => {
				opened.push(path);
				return true;
			}),
		).toBe(false);
		expect(opened).toEqual([]);
	});

	it("hands the resolved workspace path to the opener", async () => {
		const opened: string[] = [];
		expect(
			await openArtifactInEditor("/ws", "out/doc.md", "a1", async (path) => {
				opened.push(path);
				return true;
			}),
		).toBe(true);
		expect(opened).toEqual(["/ws/out/doc.md"]);
	});
});

describe("copy source", () => {
	it("writes the source to the clipboard", async () => {
		const wrote: string[] = [];
		expect(await copyArtifactSource("# hi", async (text) => void wrote.push(text))).toBe(true);
		expect(wrote).toEqual(["# hi"]);
	});

	it("returns false when the clipboard is unavailable", async () => {
		expect(
			await copyArtifactSource("x", async () => {
				throw new Error("denied");
			}),
		).toBe(false);
	});
});

describe("bound session list", () => {
	it("asks for the bound session and ignores another session's list", () => {
		bindArtifacts("s1", "/ws");
		expect(mocks.sent).toEqual([{ type: "list_artifacts", session_id: "s1" }]);
		emit({
			type: "artifact_list",
			seq: 1,
			session_id: "other",
			artifacts: [entry({ artifact_id: "nope", title: "Nope" })],
		});
		expect(artifacts.items).toEqual([]);
		emit({
			type: "artifact_list",
			seq: 1,
			session_id: "s1",
			artifacts: [entry({ artifact_id: "a1", title: "Notes", mime: "text/markdown", path: "notes.md" })],
		});
		expect(artifacts.items.map((row) => row.artifact_id)).toEqual(["a1"]);
	});

	it("appends artifact_ready for the bound session only", () => {
		bindArtifacts("s1", "/ws");
		emit({
			type: "artifact_ready",
			seq: 2,
			session_id: "other",
			artifact_id: "x",
			title: "X",
			mime: "text/plain",
			path: "x.txt",
		});
		expect(artifacts.items).toEqual([]);
		emit({
			type: "artifact_ready",
			seq: 3,
			session_id: "s1",
			artifact_id: "a2",
			title: "Later",
			mime: "text/plain",
			path: "later.txt",
		});
		expect(artifacts.items.map((row) => row.artifact_id)).toEqual(["a2"]);
	});

	it("clears the list when the bound session changes", () => {
		bindArtifacts("s1", "/ws");
		emit({
			type: "artifact_list",
			seq: 1,
			session_id: "s1",
			artifacts: [entry({ artifact_id: "a1" })],
		});
		bindArtifacts("s2", "/ws");
		expect(artifacts.items).toEqual([]);
		expect(mocks.sent.at(-1)).toEqual({ type: "list_artifacts", session_id: "s2" });
	});

	it("is a flat id list, not a tree", () => {
		bindArtifacts("s1", "/ws");
		emit({
			type: "artifact_list",
			seq: 1,
			session_id: "s1",
			artifacts: [
				entry({ artifact_id: "a1", path: "docs/a.md" }),
				entry({ artifact_id: "a2", path: "docs/b.md" }),
			],
		});
		expect(artifacts.items.every((row) => !("children" in row))).toBe(true);
		expect(artifacts.items.map((row) => row.path)).toEqual(["docs/a.md", "docs/b.md"]);
	});
});

describe("open preview", () => {
	beforeEach(() => {
		setArtifactSourceReader(async ({ path }) => `source:${path}`);
		bindArtifacts("s1", "/ws");
		emit({
			type: "artifact_list",
			seq: 1,
			session_id: "s1",
			artifacts: [
				entry({ artifact_id: "md", title: "Doc", mime: "text/markdown", path: "notes.md" }),
				entry({ artifact_id: "html", title: "Prev", mime: "text/html", path: "artifacts/html" }),
				entry({ artifact_id: "py", title: "Script", mime: "text/x-python", path: "main.py" }),
			],
		});
		mocks.sent.length = 0;
	});

	it("sends open_artifact and fills preview from the approved reader", async () => {
		expect(selectArtifact("md")).toBe(true);
		expect(mocks.sent).toEqual([{ type: "open_artifact", session_id: "s1", artifact_id: "md" }]);
		emit({
			type: "artifact",
			seq: 1,
			session_id: "s1",
			artifact_id: "md",
			title: "Doc",
			mime: "text/markdown",
			path: "notes.md",
		});
		await flush();
		expect(artifacts.preview?.kind).toBe("markdown");
		expect(artifacts.preview?.source).toBe("source:notes.md");
	});

	it("classifies html and code previews from the open reply", async () => {
		selectArtifact("html");
		emit({
			type: "artifact",
			seq: 1,
			session_id: "s1",
			artifact_id: "html",
			title: "Prev",
			mime: "text/html",
			path: "artifacts/html",
		});
		await flush();
		expect(artifacts.preview?.kind).toBe("html");
		expect(isWorkspaceArtifact("artifacts/html", "html")).toBe(false);

		selectArtifact("py");
		emit({
			type: "artifact",
			seq: 1,
			session_id: "s1",
			artifact_id: "py",
			title: "Script",
			mime: "text/x-python",
			path: "main.py",
		});
		await flush();
		expect(artifacts.preview?.kind).toBe("code");
	});

	it("ignores an open reply for a different session", async () => {
		selectArtifact("md");
		emit({
			type: "artifact",
			seq: 1,
			session_id: "other",
			artifact_id: "md",
			title: "Doc",
			mime: "text/markdown",
			path: "notes.md",
		});
		await flush();
		expect(artifacts.preview).toBeNull();
	});
});
