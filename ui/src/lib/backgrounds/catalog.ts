// The backgrounds a person can pick (TD-5202), in the order Settings →
// Appearance shows their tiles. Classic stays the default so nobody's
// window changes on upgrade; Off paints the plain ground and schedules
// nothing at all.

export const BACKGROUND_IDS = [
	"off",
	"classic",
	"galaxy",
	"deep-space",
	"aurora",
	"constellations",
	"fireflies",
] as const;

export type BackgroundId = (typeof BACKGROUND_IDS)[number];

export const DEFAULT_BACKGROUND: BackgroundId = "classic";

export interface BackgroundInfo {
	id: BackgroundId;
	label: string;
	/** One line for the hint under the tiles. */
	blurb: string;
}

export const BACKGROUNDS: readonly BackgroundInfo[] = [
	{ id: "off", label: "Off", blurb: "The plain ground colour. Nothing moves and clicks don't ripple." },
	{ id: "classic", label: "Classic", blurb: "The orbit halo, drifting dust and light under the header." },
	{ id: "galaxy", label: "Galaxy", blurb: "A spiral galaxy that turns about once every ten minutes." },
	{ id: "deep-space", label: "Deep space", blurb: "Stars drift toward you, with the odd shooting star." },
	{ id: "aurora", label: "Aurora", blurb: "Ribbons of light ripple slowly across the top of the window." },
	{ id: "constellations", label: "Constellations", blurb: "Stars wander and join up with faint lines." },
	{ id: "fireflies", label: "Fireflies", blurb: "A few warm lights wander and blink." },
];

export function isBackgroundId(value: unknown): value is BackgroundId {
	return typeof value === "string" && (BACKGROUND_IDS as readonly string[]).includes(value);
}

export function backgroundInfo(id: BackgroundId): BackgroundInfo {
	return BACKGROUNDS.find((b) => b.id === id) ?? BACKGROUNDS[1];
}
