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
	/** Disclosure affordance (workspace menu). */
	'chevron-down': '<path d="m6 9 6 6 6-6"/>',
} as const;

export type IconName = keyof typeof ICONS;
