// @vitest-environment jsdom
//
// Hold-to-talk (TD-4701): the mic opens only while held, transcription
// goes through the daemon, and the Web Speech API is not used.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
	sendOk: true,
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
		return mocks.sendOk;
	},
}));

import { settings } from "./settings.svelte.js";
import {
	COPY,
	beginHold,
	endHold,
	onTranscript,
	resetDictation,
	startDictation,
	dictation,
} from "./dictation.svelte.js";

const trackStop = vi.fn();

class FakeRecorder {
	state = "inactive";
	mimeType = "audio/webm";
	ondataavailable: ((event: { data: Blob }) => void) | null = null;
	onstop: (() => void) | null = null;
	start(): void {
		this.state = "recording";
	}
	stop(): void {
		this.state = "inactive";
		this.ondataavailable?.({
			data: new Blob([new Uint8Array([1, 2, 3])], { type: "audio/webm" }),
		});
		this.onstop?.();
	}
	static isTypeSupported(_mime: string): boolean {
		return true;
	}
}

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

beforeEach(() => {
	mocks.handler = null;
	mocks.sent = [];
	mocks.sendOk = true;
	trackStop.mockReset();
	resetDictation();
	settings.speechEnabled = false;
	settings.speechReady = false;
	vi.stubGlobal("MediaRecorder", FakeRecorder);
	vi.stubGlobal("navigator", {
		mediaDevices: {
			getUserMedia: vi.fn(async () => ({
				getTracks: () => [{ stop: trackStop }],
			})),
		},
	});
});

afterEach(() => {
	resetDictation();
	vi.unstubAllGlobals();
});

describe("hold-to-talk", () => {
	it("does nothing when speech is off", async () => {
		await beginHold();
		expect(dictation.holding).toBe(false);
		expect(mocks.sent).toEqual([]);
		expect(trackStop).not.toHaveBeenCalled();
	});

	it("does not open the mic when enabled but unconfigured", async () => {
		settings.speechEnabled = true;
		settings.speechReady = false;
		await beginHold();
		expect(dictation.error).toBe(COPY.unconfigured);
		expect(dictation.holding).toBe(false);
		expect(
			(navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>).mock.calls,
		).toEqual([]);
	});

	it("opens the mic only while held, then transcribes through the daemon", async () => {
		settings.speechEnabled = true;
		settings.speechReady = true;
		startDictation();
		const heard: string[] = [];
		onTranscript((text) => heard.push(text));
		await beginHold();
		expect(dictation.holding).toBe(true);
		expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledWith({
			audio: true,
			video: false,
		});
		await endHold();
		expect(dictation.holding).toBe(false);
		expect(trackStop).toHaveBeenCalled();
		expect(dictation.transcribing).toBe(true);
		expect(mocks.sent).toHaveLength(1);
		const msg = mocks.sent[0];
		expect(msg.type).toBe("transcribe");
		if (msg.type === "transcribe") {
			expect(msg.mime).toBe("audio/webm");
			expect(msg.audio_b64.length).toBeGreaterThan(0);
		}
		emit({ type: "transcript", seq: 1, ok: true, text: "hello from the mic" });
		expect(heard).toEqual(["hello from the mic"]);
		expect(dictation.transcribing).toBe(false);
	});

	it("maps a failed transcript to copy that never includes a URL", async () => {
		settings.speechEnabled = true;
		settings.speechReady = true;
		startDictation();
		await beginHold();
		await endHold();
		emit({
			type: "transcript",
			seq: 1,
			ok: false,
			detail: "speech_failed",
			text: "",
		});
		expect(dictation.error).toBe(COPY.failed);
		expect(dictation.error).not.toMatch(/https?:\/\//);
	});

	it("never references the Web Speech API", () => {
		const src = readFileSync(resolve(process.cwd(), "src/lib/dictation.svelte.ts"), "utf-8");
		const button = readFileSync(
			resolve(process.cwd(), "src/lib/components/chat/DictationButton.svelte"),
			"utf-8",
		);
		for (const text of [src, button]) {
			expect(text).not.toMatch(/webkitSpeechRecognition|SpeechRecognition/);
		}
	});
});
