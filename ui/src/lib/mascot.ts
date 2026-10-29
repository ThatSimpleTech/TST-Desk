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

export type Gaze = "ahead" | "left" | "right";

export interface EyeState {
	gaze: Gaze;
	shut: boolean;
}

const SHIFT: Record<Gaze, number> = { ahead: 0, left: -1, right: 1 };

/** The body as a grid of rows, `#` filled and `.` a hole. */
export function bodyRows({ gaze, shut }: EyeState): string[] {
	const grid = BODY.map((row) => row.split(""));
	const eye = shut ? EYE_SHUT : EYE_OPEN;
	for (const column of EYE_COLUMNS) {
		eye.forEach((line, dy) => {
			[...line].forEach((cell, dx) => {
				if (cell === "#") grid[EYE_TOP - BODY_TOP + dy][column + SHIFT[gaze] + dx] = ".";
			});
		});
	}
	return grid.map((row) => row.join(""));
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
