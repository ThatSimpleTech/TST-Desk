// Shared icon map (TD-1608).
//
// Every glyph in the chrome is defined here exactly once and rendered through
// Icon.svelte — no per-call-site SVG pasting. Lucide-style geometry: 24px
// viewBox, outline paths stroked in currentColor at ≈1.5px with round caps
// and joins; fills are reserved for active states (Icon.svelte's `filled`).
// Values are inner-SVG markup, injected by Icon.svelte — keep them static
// and trusted, never derived from user input.

export const ICONS = {
	/** Decisions ledger (header). */
	scroll:
		'<path d="M19 17V5a2 2 0 0 0-2-2H4"/>' +
		'<path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/>',
	/** Doctor diagnostics (header). */
	stethoscope:
		'<path d="M6 3H5a2 2 0 0 0-2 2v4a6 6 0 0 0 12 0V5a2 2 0 0 0-2-2h-1"/>' +
		'<path d="M9 15v1a6 6 0 0 0 12 0v-3"/>' +
		'<circle cx="21" cy="11" r="2"/>',
	/** Setup wizard (header). */
	settings:
		'<circle cx="12" cy="12" r="3"/>' +
		'<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/>',
	/** Workspace folder (title bar, picker menu). */
	folder:
		'<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
	/** Close / remove. */
	x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
	/** Confirm / active state / passing check. */
	check: '<path d="M20 6 9 17l-5-5"/>',
	/** Skipped check (doctor). */
	minus: '<path d="M5 12h14"/>',
	/** Warning (stack panel flags). */
	alert:
		'<path d="M10.3 3.6 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.6a2 2 0 0 0-3.4 0Z"/>' +
		'<path d="M12 9v4"/>' +
		'<path d="M12 17h.01"/>',
	/** Send message (composer). */
	'arrow-up': '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
	/** Stop the running turn (composer morph). Rendered filled. */
	stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
	/** Disclosure affordance (workspace menu). */
	'chevron-down': '<path d="m6 9 6 6 6-6"/>',
	/** Back to the project list (TD-2801). */
	'chevron-left': '<path d="m15 18-6-6 6-6"/>',
	/** Copy (message actions). */
	copy:
		'<rect x="9" y="9" width="13" height="13" rx="2"/>' +
		'<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
	/** Retry / resend (message actions). */
	'retry':
		'<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>' +
		'<path d="M3 3v5h5"/>',
	/** Sidebar / session rail toggle (TD-1701). */
	'panel-left':
		'<rect x="3" y="3" width="18" height="18" rx="2"/>' + '<path d="M9 3v18"/>',
	/** New session (session rail, TD-1701). */
	plus: '<path d="M5 12h14"/>' + '<path d="M12 5v14"/>',
	/** Filter field affordance (session rail, TD-1701). */
	search: '<circle cx="11" cy="11" r="8"/>' + '<path d="m21 21-4.3-4.3"/>',
	/** Resolved instruction stack (command palette, TD-1707). */
	layers:
		'<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.91a1 1 0 0 0 0-1.83Z"/>' +
		'<path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/>' +
		'<path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
	/** Theme toggle (command palette, TD-1707). */
	moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
	/** A session (command palette, TD-1707). */
	'message-square': '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
	/** Home surface (rail functions, TD-1712). */
	home:
		'<path d="M3 10.2 12 3l9 7.2V20a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>' +
		'<path d="M9 22v-8h6v8"/>',
	/** Scheduled surface (rail functions, TD-1712). */
	clock: '<circle cx="12" cy="12" r="9"/>' + '<path d="M12 7v5l3 2"/>',
	/** Account anchor avatar fallback (rail footer, TD-1712). */
	user: '<circle cx="12" cy="8" r="4"/>' + '<path d="M4 21a8 8 0 0 1 16 0"/>',
	/** Archive / unarchive a session (rail row actions, TD-1715). */
	archive:
		'<rect x="2" y="3" width="20" height="5" rx="1"/>' +
		'<path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/>' +
		'<path d="M10 12h4"/>',
	/** Delete a session (rail row actions, TD-1715). */
	trash:
		'<path d="M3 6h18"/>' +
		'<path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/>' +
		'<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>' +
		'<path d="M10 11v6"/><path d="M14 11v6"/>',
	/** Row action menu affordance (rail rows, TD-1715). */
	/** Attach a text file (composer, TD-1709). */
	paperclip:
		'<path d="M21.4 11.05 12.25 20.2a6 6 0 0 1-8.49-8.49l9.2-9.19a4 4 0 0 1 5.65 5.66l-9.2 9.19a2 2 0 0 1-2.82-2.83l8.48-8.49"/>',
	/** One attached file, in a chip (TD-1709). */
	file:
		'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/>' +
		'<path d="M14 2v6h6"/>',
	ellipsis:
		'<circle cx="5" cy="12" r="1"/>' +
		'<circle cx="12" cy="12" r="1"/>' +
		'<circle cx="19" cy="12" r="1"/>',
} as const;

export type IconName = keyof typeof ICONS;
