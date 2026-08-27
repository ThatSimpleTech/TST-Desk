// Hold-to-talk dictation (TD-4701).
//
// The mic is opened only while the control is held, then the tracks are
// stopped. There is no Web Speech API path: on Linux/Chromium that can
// hit a cloud recognizer. Transcription goes through the daemon so the
// host comes from speech.base_url in config. Off until setup_state says
// speech_enabled; speech_ready means a base_url is configured.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion } from "./protocol";
import { settings } from "./settings.svelte.js";

export const MAX_HOLD_MS = 30_000;
export const TRANSCRIBE_TIMEOUT_MS = 35_000;

export const COPY = {
	unconfigured:
		"Set speech.base_url in config.yaml to a transcription endpoint. There is no cloud default.",
	denied: "Microphone permission was denied.",
	unsupported: "This window cannot record audio.",
	disconnected: "Not connected to the daemon.",
	timeout: "Transcription timed out.",
	tooLarge: "That recording is too large to send.",
	badAudio: "The recording could not be transcribed.",
	auth: "The speech credential is missing from the keychain.",
	failed: "Transcription failed.",
	disabled: "Dictation is off.",
} as const;

export const dictation = $state({
	holding: false,
	transcribing: false,
	error: null as string | null,
});

const listeners = new Set<(text: string) => void>();
let refs = 0;
let offEvent: (() => void) | null = null;
let media: MediaStream | null = null;
let recorder: MediaRecorder | null = null;
let chunks: Blob[] = [];
let holdTimer: ReturnType<typeof setTimeout> | null = null;
let replyTimer: ReturnType<typeof setTimeout> | null = null;
let inFlight = false;

export function onTranscript(handler: (text: string) => void): () => void {
	listeners.add(handler);
	return () => {
		listeners.delete(handler);
	};
}

/** Register the transcript reducer. Unsubscribe for tests. */
export function startDictation(): () => void {
	refs += 1;
	if (refs === 1) {
		offEvent = onEvent(reduce);
	}
	return () => {
		refs -= 1;
		if (refs > 0) return;
		refs = 0;
		offEvent?.();
		offEvent = null;
		cancelHold();
		clearReplyTimer();
		inFlight = false;
		dictation.transcribing = false;
	};
}

export function resetDictation(): void {
	offEvent?.();
	offEvent = null;
	refs = 0;
	cancelHold();
	clearReplyTimer();
	inFlight = false;
	dictation.holding = false;
	dictation.transcribing = false;
	dictation.error = null;
	listeners.clear();
}

export async function beginHold(): Promise<void> {
	dictation.error = null;
	if (!settings.speechEnabled) return;
	if (!settings.speechReady) {
		dictation.error = COPY.unconfigured;
		return;
	}
	if (dictation.holding || dictation.transcribing || inFlight) return;
	if (typeof navigator === "undefined" || navigator.mediaDevices?.getUserMedia == null) {
		dictation.error = COPY.unsupported;
		return;
	}
	if (typeof MediaRecorder === "undefined") {
		dictation.error = COPY.unsupported;
		return;
	}
	let stream: MediaStream;
	try {
		stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
	} catch {
		dictation.error = COPY.denied;
		return;
	}
	media = stream;
	chunks = [];
	const mime = pickMime();
	try {
		recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
	} catch {
		stopTracks(stream);
		media = null;
		dictation.error = COPY.unsupported;
		return;
	}
	recorder.ondataavailable = (event: BlobEvent) => {
		if (event.data.size > 0) chunks.push(event.data);
	};
	recorder.start();
	dictation.holding = true;
	holdTimer = setTimeout(() => {
		void endHold();
	}, MAX_HOLD_MS);
}

export async function endHold(): Promise<void> {
	if (!dictation.holding && recorder === null) return;
	dictation.holding = false;
	clearHoldTimer();
	const rec = recorder;
	recorder = null;
	const stream = media;
	media = null;
	if (rec === null) {
		chunks = [];
		stopTracks(stream);
		return;
	}
	await stopRecorder(rec);
	const recorded = chunks;
	chunks = [];
	stopTracks(stream);
	const mime = rec.mimeType || "audio/webm";
	const blob = new Blob(recorded, { type: mime });
	if (blob.size === 0) return;
	await sendBlob(blob);
}

export function cancelHold(): void {
	dictation.holding = false;
	clearHoldTimer();
	const rec = recorder;
	recorder = null;
	const stream = media;
	media = null;
	chunks = [];
	if (rec !== null && rec.state !== "inactive") {
		try {
			rec.stop();
		} catch {
			// Already stopped.
		}
	}
	stopTracks(stream);
}

function reduce(event: DaemonEventUnion): void {
	if (event.type !== "transcript") return;
	if (!inFlight) return;
	inFlight = false;
	dictation.transcribing = false;
	clearReplyTimer();
	if (!event.ok) {
		dictation.error = userCopy(event.detail ?? "");
		return;
	}
	const text = (event.text ?? "").trim();
	if (text) {
		for (const handler of listeners) handler(text);
	}
}

function userCopy(detail: string): string {
	switch (detail) {
		case "speech_disabled":
			return COPY.disabled;
		case "speech_unconfigured":
			return COPY.unconfigured;
		case "speech_too_large":
			return COPY.tooLarge;
		case "speech_bad_audio":
			return COPY.badAudio;
		case "speech_auth":
			return COPY.auth;
		case "speech_failed":
			return COPY.failed;
		default:
			return COPY.failed;
	}
}

function pickMime(): string | undefined {
	const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
	return candidates.find((item) => MediaRecorder.isTypeSupported(item));
}

function stopTracks(stream: MediaStream | null): void {
	if (stream === null) return;
	for (const track of stream.getTracks()) track.stop();
}

function stopRecorder(rec: MediaRecorder): Promise<void> {
	return new Promise((resolve) => {
		if (rec.state === "inactive") {
			resolve();
			return;
		}
		rec.onstop = () => {
			resolve();
		};
		rec.stop();
	});
}

async function sendBlob(blob: Blob): Promise<void> {
	const buffer = await blob.arrayBuffer();
	const bytes = new Uint8Array(buffer);
	dictation.transcribing = true;
	inFlight = true;
	const sent = sendToDaemon({
		type: "transcribe",
		audio_b64: bytesToB64(bytes),
		mime: blob.type || "audio/webm",
	});
	if (!sent) {
		inFlight = false;
		dictation.transcribing = false;
		dictation.error = COPY.disconnected;
		return;
	}
	replyTimer = setTimeout(() => {
		if (!inFlight) return;
		inFlight = false;
		dictation.transcribing = false;
		dictation.error = COPY.timeout;
	}, TRANSCRIBE_TIMEOUT_MS);
}

function bytesToB64(bytes: Uint8Array): string {
	let binary = "";
	const chunk = 0x8000;
	for (let offset = 0; offset < bytes.length; offset += chunk) {
		binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
	}
	return btoa(binary);
}

function clearHoldTimer(): void {
	if (holdTimer === null) return;
	clearTimeout(holdTimer);
	holdTimer = null;
}

function clearReplyTimer(): void {
	if (replyTimer === null) return;
	clearTimeout(replyTimer);
	replyTimer = null;
}
