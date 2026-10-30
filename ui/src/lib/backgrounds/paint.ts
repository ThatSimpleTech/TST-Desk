// Canvas plumbing the backgrounds share (TD-5202): the pixel-ratio caps,
// reading colours out of the design tokens, the theme's blend mode, and
// watching for a theme switch or a new screen density so a scene can
// recolour or resize without a reload.
//
// Colours are never written here. Every background reads its palette from
// --bg-* tokens in tokens.css, which define a light and a dark value, so a
// theme switch is a token change like any other (AGENTS §6).

/** Past 2× the extra pixels cost fill rate and buy nothing a soft ambient
 *  layer can show. Preview tiles are small, so they keep it. */
export const DPR_CAP = 2;

/** A layer behind the whole window clears and redraws every pixel on every
 *  paint. Where the canvas is drawn in software (a Linux webview with no
 *  GPU canvas), a 2× backing store of a large window is four times the
 *  fill of 1× and took a fifth of the UI thread; at 1.5× it is a little
 *  over half of 2×, and a soft, veiled star field still reads sharp. */
export const WINDOW_DPR_CAP = 1.5;

/** Backing-store pixels per CSS pixel: the device ratio, capped at `cap`,
 *  times the scene's own resolution (soft scenes draw small and let the
 *  browser's smoothing blur them up). */
export function canvasScale(devicePixelRatio: number | undefined, resolution = 1, cap = DPR_CAP): number {
	const dpr = Number.isFinite(devicePixelRatio) && (devicePixelRatio ?? 0) > 0 ? (devicePixelRatio as number) : 1;
	const res = Number.isFinite(resolution) && resolution > 0 ? resolution : 1;
	const most = Number.isFinite(cap) && cap > 0 ? cap : DPR_CAP;
	return Math.max(Math.min(dpr, most) * res, 0.05);
}

/** Size the backing store for `width`×`height` CSS px. Returns whether it
 *  changed: resizing a canvas clears it, so callers repaint after. */
export function fitCanvas(
	canvas: { width: number; height: number },
	width: number,
	height: number,
	scale: number,
): boolean {
	const w = Math.max(1, Math.round(width * scale));
	const h = Math.max(1, Math.round(height * scale));
	if (canvas.width === w && canvas.height === h) return false;
	canvas.width = w;
	canvas.height = h;
	return true;
}

export type Rgb = readonly [number, number, number];

export interface ParsedColor {
	rgb: Rgb;
	alpha: number;
}

const HEX = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const RGB_FN = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/i;

function clampByte(n: number): number {
	return Math.min(255, Math.max(0, Math.round(n)));
}

/** A token value as RGB plus alpha: #rgb, #rrggbb, #rrggbbaa, rgb() and
 *  rgba() in comma or space syntax. Null for anything else. */
export function parseColor(value: string): ParsedColor | null {
	const text = value.trim();
	const hex = HEX.exec(text);
	if (hex) {
		let digits = hex[1];
		if (digits.length === 3) digits = [...digits].map((c) => c + c).join("");
		const n = parseInt(digits.slice(0, 6), 16);
		const alpha = digits.length === 8 ? parseInt(digits.slice(6), 16) / 255 : 1;
		return { rgb: [(n >> 16) & 255, (n >> 8) & 255, n & 255], alpha };
	}
	const fn = RGB_FN.exec(text);
	if (fn) {
		const rawAlpha = fn[4];
		let alpha = 1;
		if (rawAlpha !== undefined) {
			alpha = rawAlpha.endsWith("%") ? parseFloat(rawAlpha) / 100 : parseFloat(rawAlpha);
		}
		return {
			rgb: [clampByte(parseFloat(fn[1])), clampByte(parseFloat(fn[2])), clampByte(parseFloat(fn[3]))],
			alpha: Math.min(1, Math.max(0, alpha)),
		};
	}
	return null;
}

/** A CSS colour string for a parsed token at `alpha` (times its own). Used
 *  when a sprite or gradient is built, never per frame. */
export function cssColor(color: ParsedColor, alpha = 1): string {
	const [r, g, b] = color.rgb;
	const a = Math.min(1, Math.max(0, color.alpha * alpha));
	return `rgba(${r},${g},${b},${Number(a.toFixed(4))})`;
}

const TRANSPARENT: ParsedColor = { rgb: [0, 0, 0], alpha: 0 };

/** Read a colour token, transparent if it is missing or unreadable — a
 *  broken token hides a layer instead of painting it black. */
export function tokenColor(read: (name: string) => string, name: string): ParsedColor {
	return parseColor(read(name)) ?? TRANSPARENT;
}

/** Read a 0–1 number token (an opacity), `fallback` if it is not one. */
export function tokenNumber(read: (name: string) => string, name: string, fallback: number): number {
	const n = parseFloat(read(name));
	return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : fallback;
}

/** The blend modes a theme may ask for in --bg-ambient-blend. Dark adds
 *  light ("lighter"); paper can only be inked ("source-over"). */
const BLENDS = ["source-over", "lighter", "screen", "multiply"] as const;
export type Blend = (typeof BLENDS)[number];

export function blendMode(value: string): Blend {
	const text = value.trim();
	return (BLENDS as readonly string[]).includes(text) ? (text as Blend) : "source-over";
}

/** Reads custom properties as they resolve on `el`, so the theme the
 *  element sits under is the one that answers. */
export function tokenReader(el: Element): (name: string) => string {
	return (name) => getComputedStyle(el).getPropertyValue(name).trim();
}

/** Call back when the theme changes: an explicit choice stamps data-theme
 *  on <html>, and "system" follows the OS colour scheme. */
export function watchTheme(win: Window, run: () => void): () => void {
	const root = win.document.documentElement;
	const observer =
		typeof MutationObserver === "function"
			? new MutationObserver(() => run())
			: null;
	observer?.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
	let scheme: MediaQueryList | null = null;
	try {
		scheme = typeof win.matchMedia === "function" ? win.matchMedia("(prefers-color-scheme: dark)") : null;
	} catch {
		scheme = null;
	}
	scheme?.addEventListener("change", run);
	return () => {
		observer?.disconnect();
		scheme?.removeEventListener("change", run);
	};
}

/** Call back when the device pixel ratio changes while the size does not
 *  (the window dragged to a screen of another density): no ResizeObserver
 *  reports that, and the canvas would stay at the old scale. A query
 *  matches only the ratio it was made for, so it is made again for the new
 *  one after each change. */
export function watchPixelRatio(win: Window, run: () => void): () => void {
	let query: MediaQueryList | null = null;
	let stopped = false;
	const onChange = (): void => {
		disarm();
		if (stopped) return;
		run();
		arm();
	};
	function arm(): void {
		try {
			query = typeof win.matchMedia === "function" ? win.matchMedia(`(resolution: ${win.devicePixelRatio}dppx)`) : null;
		} catch {
			query = null;
		}
		query?.addEventListener("change", onChange);
	}
	function disarm(): void {
		query?.removeEventListener("change", onChange);
		query = null;
	}
	arm();
	return () => {
		stopped = true;
		disarm();
	};
}

/** A soft round glow, `size` px square, fading from `color` at the centre
 *  to nothing at the rim. Built once per theme; scenes stamp it with
 *  drawImage, which is far cheaper than a gradient per star per frame.
 *  Null where there is no 2D canvas (tests). */
export function glowSprite(doc: Document, size: number, color: ParsedColor, core = 0.15): HTMLCanvasElement | null {
	const sprite = doc.createElement("canvas");
	sprite.width = size;
	sprite.height = size;
	const ctx = sprite.getContext("2d");
	if (ctx === null) return null;
	const half = size / 2;
	const gradient = ctx.createRadialGradient(half, half, 0, half, half, half);
	gradient.addColorStop(0, cssColor(color, 1));
	gradient.addColorStop(core, cssColor(color, 0.8));
	gradient.addColorStop(0.45, cssColor(color, 0.22));
	gradient.addColorStop(1, cssColor(color, 0));
	ctx.fillStyle = gradient;
	ctx.fillRect(0, 0, size, size);
	return sprite;
}
