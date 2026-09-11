// Hold-to-talk dictation (TD-4701). Pure helpers — the mic and the
// daemon live in the store. Off by default. No always-on capture.
//
// Two paths, both engine-agnostic: they fill the composer, they do not
// talk to the agent loop. OS SpeechRecognition when the webview has it;
// otherwise a clip posted as `transcribe` when config names an endpoint.

export const MAX_AUDIO_BYTES = 384_000;

// `object` rather than a shape with two optional keys: lib.dom declares
// neither SpeechRecognition name, so TypeScript's weak-type check refused
// to pass `window` in. The `in` probes below need nothing more than an object.
export function osDictationAvailable(
	win: object | undefined = typeof window === "undefined" ? undefined : window,
): boolean {
	if (win === undefined) return false;
	return "SpeechRecognition" in win || "webkitSpeechRecognition" in win;
}

export function canDictate(input: {
	enabled: boolean;
	osAvailable: boolean;
	hasEndpoint: boolean;
}): boolean {
	return input.enabled && (input.osAvailable || input.hasEndpoint);
}

export function dictationHint(input: {
	enabled: boolean;
	osAvailable: boolean;
	hasEndpoint: boolean;
}): string {
	if (!input.enabled) {
		return "Dictation is off. Turn it on in Settings → Appearance.";
	}
	if (input.osAvailable) {
		return "Hold to talk. Uses OS dictation. Nothing is sent until you let go.";
	}
	if (input.hasEndpoint) {
		return "Hold to talk. The clip is sent to your configured transcription endpoint.";
	}
	return "No dictation on this system. Enable OS dictation, or set voice.base_url in config.yaml.";
}

export function appendTranscript(existing: string, spoken: string): string {
	const clip = spoken.trim();
	if (!clip) return existing;
	if (!existing) return clip;
	if (existing.endsWith(" ") || existing.endsWith("\n")) return existing + clip;
	return `${existing} ${clip}`;
}

export function audioTooLarge(bytes: number): boolean {
	return bytes > MAX_AUDIO_BYTES;
}
