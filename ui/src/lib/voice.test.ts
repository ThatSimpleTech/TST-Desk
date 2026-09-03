import { describe, expect, it } from "vitest";
import {
	MAX_AUDIO_BYTES,
	appendTranscript,
	audioTooLarge,
	canDictate,
	dictationHint,
	osDictationAvailable,
} from "./voice";

describe("osDictationAvailable", () => {
	it("is false without a window", () => {
		expect(osDictationAvailable(undefined)).toBe(false);
	});

	it("is true when webkitSpeechRecognition exists", () => {
		expect(osDictationAvailable({ webkitSpeechRecognition: function Webkit() {} })).toBe(true);
	});
});

describe("canDictate", () => {
	it("is off until Settings turns it on", () => {
		expect(canDictate({ enabled: false, osAvailable: true, hasEndpoint: true })).toBe(false);
	});

	it("takes OS dictation or a configured endpoint", () => {
		expect(canDictate({ enabled: true, osAvailable: true, hasEndpoint: false })).toBe(true);
		expect(canDictate({ enabled: true, osAvailable: false, hasEndpoint: true })).toBe(true);
		expect(canDictate({ enabled: true, osAvailable: false, hasEndpoint: false })).toBe(false);
	});
});

describe("dictationHint", () => {
	it("names Settings when off", () => {
		expect(dictationHint({ enabled: false, osAvailable: true, hasEndpoint: false })).toMatch(
			/Settings/,
		);
	});

	it("does not claim a send while holding OS dictation", () => {
		expect(dictationHint({ enabled: true, osAvailable: true, hasEndpoint: false })).not.toContain(
			"was sent",
		);
	});
});

describe("appendTranscript", () => {
	it("inserts a space between existing draft and the clip", () => {
		expect(appendTranscript("Fix the", "login page")).toBe("Fix the login page");
		expect(appendTranscript("", "hello")).toBe("hello");
		expect(appendTranscript("hello ", "there")).toBe("hello there");
	});
});

describe("audioTooLarge", () => {
	it("matches the daemon cap", () => {
		expect(audioTooLarge(MAX_AUDIO_BYTES)).toBe(false);
		expect(audioTooLarge(MAX_AUDIO_BYTES + 1)).toBe(true);
	});
});
