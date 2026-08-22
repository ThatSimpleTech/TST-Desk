// Tests for the Charter column (TD-4002).

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";
import { charterEmptyCopy, charterLedeCopy, charterPayload, previewCharterYaml } from "./charter";

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
	addListItem,
	charter,
	loadCharter,
	resetCharter,
	saveCharter,
	setListItem,
	setObjective,
	startCharter,
} from "./charter.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

const sampleFields = {
	objective: "Ship the importer.",
	definition_of_done: ["tests pass"],
	source_of_truth: ["docs/spec.md"],
	boundary: {
		writable_paths: ["src/**"],
		allowed_commands: ["pytest"],
		network: "deny" as const,
	},
	caps: {
		spend_usd: 12,
		wall_clock_hours: 4,
		max_iterations: 80,
	},
	stop_conditions: ["any Class C decision"],
};

beforeEach(() => {
	mocks.sent.length = 0;
	resetCharter();
	startCharter();
	mocks.sent.length = 0;
});

afterEach(() => {
	resetCharter();
});

describe("copy", () => {
	it("points the empty state at spec §12.4", () => {
		expect(charterEmptyCopy()).toMatch(/§12\.4/);
		expect(charterEmptyCopy()).toMatch(/No charter yet/);
	});

	it("names spec §12.4 in the lede", () => {
		expect(charterLedeCopy()).toMatch(/§12\.4/);
	});
});

describe("payload", () => {
	it("drops blank list rows before save", () => {
		const payload = charterPayload({
			...sampleFields,
			definition_of_done: ["tests pass", "  "],
			source_of_truth: [""],
		});
		expect(payload.definition_of_done).toEqual(["tests pass"]);
		expect(payload.source_of_truth).toEqual([]);
	});

	it("is structured fields, not a YAML string", () => {
		const payload = charterPayload(sampleFields);
		expect(payload.objective).toBe("Ship the importer.");
		expect(payload.boundary).toEqual(sampleFields.boundary);
	});
});

describe("preview", () => {
	it("is a frontmatter preview, not the only editor input", () => {
		const yaml = previewCharterYaml(sampleFields, "Notes.");
		expect(yaml).toMatch(/^---\n/);
		expect(yaml).toMatch(/objective:/);
		expect(yaml).toMatch(/Notes\./);
	});
});

describe("store", () => {
	it("asks the daemon for this workspace", () => {
		loadCharter("/ws");
		expect(mocks.sent).toEqual([{ type: "get_charter", workspace_path: "/ws" }]);
	});

	it("fills the draft from a present charter", () => {
		loadCharter("/ws");
		emit({
			type: "charter",
			seq: 1,
			workspace_path: "/ws",
			present: true,
			charter: sampleFields,
			notes: "Human context.",
		});
		expect(charter.present).toBe(true);
		expect(charter.draft.objective).toBe("Ship the importer.");
		expect(charter.notes).toBe("Human context.");
	});

	it("keeps the empty form when the file is absent", () => {
		loadCharter("/ws");
		emit({
			type: "charter",
			seq: 1,
			workspace_path: "/ws",
			present: false,
			charter: null,
			notes: "",
		});
		expect(charter.present).toBe(false);
		expect(charter.draft.objective).toBe("");
		expect(charterEmptyCopy()).toMatch(/§12\.4/);
	});

	it("ignores a document for a different workspace", () => {
		loadCharter("/ws");
		emit({
			type: "charter",
			seq: 1,
			workspace_path: "/other",
			present: true,
			charter: sampleFields,
		});
		expect(charter.present).toBe(false);
		expect(charter.draft.objective).toBe("");
	});

	it("saves structured fields through save_charter", () => {
		loadCharter("/ws");
		emit({
			type: "charter",
			seq: 1,
			workspace_path: "/ws",
			present: false,
			charter: null,
		});
		setObjective("Ship the importer.");
		setListItem("definition_of_done", 0, "tests pass");
		addListItem("source_of_truth");
		setListItem("source_of_truth", 0, "docs/spec.md");
		expect(saveCharter()).toBe(true);
		expect(mocks.sent.at(-1)).toEqual({
			type: "save_charter",
			workspace_path: "/ws",
			charter: charterPayload(charter.draft),
			notes: "",
		});
		const sent = mocks.sent.at(-1);
		expect(sent).toBeDefined();
		if (sent !== undefined && sent.type === "save_charter") {
			expect(typeof sent.charter).toBe("object");
			expect(sent.charter).not.toBeNull();
			expect(Array.isArray(sent.charter)).toBe(false);
		}
		emit({
			type: "charter",
			seq: 1,
			workspace_path: "/ws",
			present: true,
			charter: sampleFields,
		});
		expect(charter.present).toBe(true);
		expect(charter.saving).toBe(false);
	});

	it("keeps the draft when save is refused", () => {
		loadCharter("/ws");
		setObjective("x");
		saveCharter();
		emit({
			type: "error",
			seq: 1,
			code: "invalid_charter",
			message: "source_of_truth: path outside the workspace: /etc/passwd",
		});
		expect(charter.saving).toBe(false);
		expect(charter.draft.objective).toBe("x");
		expect(charter.error).toMatch(/source_of_truth/);
	});
});
