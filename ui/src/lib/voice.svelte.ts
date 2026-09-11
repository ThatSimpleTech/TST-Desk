// Hold-to-talk wiring (TD-4701). Settings owns the on/off bit (setup_state).
// This store owns the in-flight capture and the transcript event.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion } from "./protocol";
import { settings } from "./settings.svelte.js";
import { toBase64 } from "./attachments";
import { audioTooLarge, canDictate, osDictationAvailable } from "./voice";

export const voice = $state({
	listening: false,
	error: null as string | null,
});

type Spoken = (text: string) => void;

let started = false;
let pendingSpoken: Spoken | null = null;
let recognizer: { start(): void; stop(): void; abort(): void } | null = null;
let recorder: MediaRecorder | null = null;
let chunks: Blob[] = [];
let stream: MediaStream | null = null;

export function resetVoice(): void {
	started = false;
	voice.listening = false;
	voice.error = null;
	pendingSpoken = null;
	stopCapture();
}

export function startVoice(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(onTranscript);
	return () => {
		started = false;
		off();
		stopCapture();
	};
}

function onTranscript(event: DaemonEventUnion): void {
	if (event.type !== "transcript") return;
	if (event.error) {
		voice.error = event.error;
		return;
	}
	const text = event.text ?? "";
	pendingSpoken?.(text);
	pendingSpoken = null;
}

export function beginDictation(onSpoken: Spoken): void {
	voice.error = null;
	if (
		!canDictate({
			enabled: settings.voiceEnabled,
			osAvailable: osDictationAvailable(),
			hasEndpoint: settings.voiceHasEndpoint,
		})
	) {
		return;
	}
	pendingSpoken = onSpoken;
	if (osDictationAvailable()) {
		startOsDictation(onSpoken);
		return;
	}
	void startEndpointDictation();
}

export function endDictation(): void {
	if (recognizer !== null) {
		try {
			recognizer.stop();
		} catch {
			recognizer.abort();
		}
		return;
	}
	if (recorder !== null && recorder.state === "recording") {
		recorder.stop();
		return;
	}
	voice.listening = false;
}

function startOsDictation(onSpoken: Spoken): void {
	const Ctor = (window as Window & {
		SpeechRecognition?: new () => SpeechRecognitionLike;
		webkitSpeechRecognition?: new () => SpeechRecognitionLike;
	}).SpeechRecognition ??
		(window as Window & { webkitSpeechRecognition?: new () => SpeechRecognitionLike })
			.webkitSpeechRecognition;
	if (Ctor === undefined) return;
	const rec = new Ctor();
	rec.interimResults = false;
	rec.continuous = true;
	rec.onresult = (event) => {
		const result = event.results[event.results.length - 1];
		const spoken = result?.[0]?.transcript ?? "";
		if (spoken) onSpoken(spoken);
	};
	rec.onerror = (event) => {
		if (event.error !== "aborted" && event.error !== "no-speech") {
			voice.error = "Dictation did not catch that. Try again.";
		}
		voice.listening = false;
		recognizer = null;
	};
	rec.onend = () => {
		voice.listening = false;
		recognizer = null;
	};
	recognizer = rec;
	voice.listening = true;
	rec.start();
}

async function startEndpointDictation(): Promise<void> {
	try {
		stream = await navigator.mediaDevices.getUserMedia({ audio: true });
	} catch {
		voice.error = "Microphone permission was not granted.";
		return;
	}
	chunks = [];
	const rec = new MediaRecorder(stream);
	recorder = rec;
	rec.ondataavailable = (event) => {
		if (event.data.size > 0) chunks.push(event.data);
	};
	rec.onstop = () => {
		void flushClip();
	};
	voice.listening = true;
	rec.start();
}

async function flushClip(): Promise<void> {
	voice.listening = false;
	stopStream();
	recorder = null;
	const blob = new Blob(chunks, { type: chunks[0]?.type || "audio/webm" });
	chunks = [];
	if (blob.size === 0) return;
	if (audioTooLarge(blob.size)) {
		voice.error = "That clip is too long. Hold a shorter phrase, or use OS dictation.";
		return;
	}
	const buffer = await blob.arrayBuffer();
	const bytes = new Uint8Array(buffer);
	sendToDaemon({
		type: "transcribe",
		audio_b64: toBase64(bytes),
		mime: blob.type || "audio/webm",
	});
}

function stopCapture(): void {
	if (recognizer !== null) {
		try {
			recognizer.abort();
		} catch {
			/* already stopped */
		}
		recognizer = null;
	}
	if (recorder !== null && recorder.state === "recording") {
		try {
			recorder.stop();
		} catch {
			/* already stopped */
		}
	}
	recorder = null;
	stopStream();
	voice.listening = false;
}

function stopStream(): void {
	stream?.getTracks().forEach((track) => track.stop());
	stream = null;
}

interface SpeechRecognitionLike {
	interimResults: boolean;
	continuous: boolean;
	onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
	onerror: ((event: { error: string }) => void) | null;
	onend: (() => void) | null;
	start(): void;
	stop(): void;
	abort(): void;
}


