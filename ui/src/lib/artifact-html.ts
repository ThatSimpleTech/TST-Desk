// TD-5262: the HTML an artifact preview shows is written by a model, and a
// model can be talked into writing anything. The frame already runs no
// script (an empty sandbox) and loads nothing (a `default-src 'none'`
// policy, first in the document). Two things were left: a link could send
// the frame to any address when clicked, carrying whatever the model put
// in it, and tags such as `<meta refresh>` or `<link rel=preconnect>` are
// not all governed by the policy in every webview. So the markup is
// cleaned first: nothing that navigates, loads, posts or refreshes.
//
// This is a second layer. The sandbox and the policy stay exactly as they
// were; `htmlPreviewSrcdoc` still wraps the result.

import DOMPurify from "dompurify";

import { htmlPreviewSrcdoc } from "./artifacts";

/** Shown above an HTML preview so a dead link is not a surprise. */
export const STATIC_PREVIEW_NOTE = "Static preview. Links and scripts are off.";

const FORBID_TAGS = [
	"script",
	"iframe",
	"frame",
	"frameset",
	"object",
	"embed",
	"form",
	"base",
	"meta",
	"link",
	"template",
	"noscript",
];

// Everything that names an address the frame could go to or fetch, and the
// form attributes that would post. Event handlers go by DOMPurify's default.
const FORBID_ATTR = [
	"href",
	"xlink:href",
	"ping",
	"action",
	"formaction",
	"srcset",
	"background",
	"poster",
	"target",
];

// Its own instance: the hook below must not reach markdown or mermaid.
let purifier: ReturnType<typeof DOMPurify> | null = null;

function instance(): ReturnType<typeof DOMPurify> {
	if (purifier !== null) return purifier;
	purifier = DOMPurify(window);
	// `src` survives only as an inline `data:` image. Anything else is an
	// address, and the policy's own `img-src data:` says the same.
	purifier.addHook("uponSanitizeAttribute", (_node, data) => {
		if (data.attrName === "src" && !/^data:image\//i.test(data.attrValue.trim())) {
			data.keepAttr = false;
		}
	});
	return purifier;
}

/** `html` with everything that could navigate, load, post or refresh taken
 *  out. Styles stay: the frame's policy already confines what they reach. */
export function sanitizeArtifactHtml(html: string): string {
	const root = instance().sanitize(html, {
		WHOLE_DOCUMENT: true,
		RETURN_DOM: true,
		ADD_TAGS: ["style"],
		FORBID_TAGS,
		FORBID_ATTR,
		ALLOW_DATA_ATTR: false,
		ALLOW_UNKNOWN_PROTOCOLS: false,
	}) as unknown as Element;
	// A whole document keeps its styles in the head; the preview wraps a body.
	const styles = [...root.querySelectorAll("head style")].map((el) => el.outerHTML).join("");
	return styles + (root.querySelector("body")?.innerHTML ?? "");
}

/** The `srcdoc` an HTML artifact is shown with: cleaned, then wrapped with
 *  the first-wins policy. */
export function artifactPreviewSrcdoc(html: string): string {
	return htmlPreviewSrcdoc(sanitizeArtifactHtml(html));
}
