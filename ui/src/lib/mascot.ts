// The TST mascot (TD-5190): the padlock with two eyes from the That Simple
// Tech logo, redrawn on a 24-pixel grid so it stays sharp at any whole-pixel
// scale. The eyes are holes, not painted shapes: the chat pane is
// transparent over the ambient background, so a painted eye would show as a
// flat patch. That is why each eye position is its own body outline (a
// sprite frame) instead of one moving shape.

export const MASCOT_WIDTH = 24;

/** Rows 0–8; row 9 is the gap between shackle and body. */
const SHACKLE = [
	"........########........",
	"......############......",
	".....##############.....",
	".....####......####.....",
	"....####........####....",
	"....###..........###....",
	"....###..........###....",
	"....###..........###....",
	"....###..........###....",
];

const BODY_TOP = 10;
const BODY = [
	"..####################..",
	".######################.",
	...Array<string>(11).fill("########################"),
	".######################.",
	"..####################..",
];

export const MASCOT_HEIGHT = BODY_TOP + BODY.length;

const EYE_OPEN = [".##.", "####", "####", "####", "####", ".##."];
/** A shut eye is a one-pixel slit just below the open eye's middle. */
const EYE_SHUT = ["....", "....", "....", "####", "....", "...."];
const EYE_TOP = 14;
const EYE_COLUMNS = [5, 15];
/** The row through the middle of the open eyes. */
export const EYE_ROW = EYE_TOP + EYE_OPEN.length / 2;

/** One art pixel of eye movement: left/up is -1, right/down is +1. At any
 *  one-pixel shift the body keeps a solid border around each eye. */
export type Step = -1 | 0 | 1;

export interface Gaze {
	dx: Step;
	dy: Step;
}

export interface EyeState extends Gaze {
	shut: boolean;
}

export const AHEAD: Gaze = { dx: 0, dy: 0 };

/** All nine gazes, ahead first. */
export const GAZES: readonly Gaze[] = [-1, 0, 1]
	.flatMap((dy) => [-1, 0, 1].map((dx) => ({ dx, dy }) as Gaze))
	.sort((a, b) => Math.abs(a.dx) + Math.abs(a.dy) - (Math.abs(b.dx) + Math.abs(b.dy)));

/** The body as a grid of rows, `#` filled and `.` a hole. */
export function bodyRows({ dx: shiftX, dy: shiftY, shut }: EyeState): string[] {
	const grid = BODY.map((row) => row.split(""));
	const eye = shut ? EYE_SHUT : EYE_OPEN;
	for (const column of EYE_COLUMNS) {
		eye.forEach((line, dy) => {
			[...line].forEach((cell, dx) => {
				if (cell === "#") grid[EYE_TOP - BODY_TOP + shiftY + dy][column + shiftX + dx] = ".";
			});
		});
	}
	return grid.map((row) => row.join(""));
}

/** Which way to look from `from` to see `to`, in eighths of a turn. Anything
 *  within `near` pixels is straight ahead: the cursor is on the mascot. */
export function gazeToward(from: { x: number; y: number }, to: { x: number; y: number }, near: number): Gaze {
	const x = to.x - from.x;
	const y = to.y - from.y;
	if (Math.hypot(x, y) < near) return AHEAD;
	const eighth = Math.round(Math.atan2(y, x) / (Math.PI / 4)) * (Math.PI / 4);
	// `+ 0` turns -0 into 0, so ahead always compares equal.
	return { dx: (Math.round(Math.cos(eighth)) + 0) as Step, dy: (Math.round(Math.sin(eighth)) + 0) as Step };
}

/** One rectangle per run of filled cells, so the path stays short. */
function rowsToPath(rows: readonly string[], top: number): string {
	const parts: string[] = [];
	rows.forEach((row, y) => {
		for (const run of row.matchAll(/#+/g)) {
			const x = run.index ?? 0;
			parts.push(`M${x} ${top + y}h${run[0].length}v1h-${run[0].length}z`);
		}
	});
	return parts.join("");
}

export const SHACKLE_PATH = rowsToPath(SHACKLE, 0);

export function bodyPath(eyes: EyeState): string {
	return rowsToPath(bodyRows(eyes), BODY_TOP);
}
