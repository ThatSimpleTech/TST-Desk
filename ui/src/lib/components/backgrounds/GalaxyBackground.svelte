<script lang="ts">
	// Galaxy background (TD-5202): a tilted two-armed spiral turning slowly
	// over a sky of twinkling stars. The soft light (core, arm haze, dust
	// lanes, knots of glowing gas) is painted once per theme into a face-on
	// sprite; each frame turns that sprite with one drawImage on a
	// half-resolution canvas — a glow has no detail to lose, and it is most
	// of the pixels — and places a couple of thousand crisp stars on a
	// full-resolution canvas above it. Maths in backgrounds/galaxy.ts.
	import AmbientCanvas from "./AmbientCanvas.svelte";
	import {
		armGlow,
		dustLanes,
		galaxyLayout,
		galaxyStars,
		nebulaKnots,
		projectInto,
		spinAt,
		starCount,
		TONES,
		type GlowStamp,
	} from "../../backgrounds/galaxy";
	import { skyField, twinkleAlpha } from "../../backgrounds/sky";
	import {
		blendMode,
		cssColor,
		glowSprite,
		tokenColor,
		tokenNumber,
		type ParsedColor,
	} from "../../backgrounds/paint";
	import type { BackgroundProps, Scene } from "../../backgrounds/scene";

	let { animate, fill = "window" }: BackgroundProps = $props();

	const SEED = 0x6a1a;
	const GLOW_PX = 512;
	/** The rim moves about 7 px a second: 15 frames a second is smooth, and
	 *  half the work of 30. */
	const FPS = 15;
	/** Stars this big get a soft halo instead of a square. */
	const HALO_FROM = 1.4;
	/** Opacity at the very centre of the bulge's glow. */
	const BULGE_PEAK = 0.6;
	/** The dust lanes cut light away ("destination-out" reads only alpha),
	 *  so their stamp is a mask, never a colour on screen. */
	const LANE_MASK: ParsedColor = { rgb: [0, 0, 0], alpha: 1 };

	let width = 0;
	let height = 0;
	let layout = galaxyLayout(0, 0);
	let stars = galaxyStars(SEED, 0);
	let sky = skyField({ seed: SEED, width: 0, height: 0, per100k: 0, minR: 0, maxR: 0 });
	let blend: GlobalCompositeOperation = "source-over";
	let toneFill: string[] = [];
	let skyFill = "";
	let opacity = 1;
	let halo: HTMLCanvasElement | null = null;
	const point = new Float32Array(2);

	let glowLayout = galaxyLayout(0, 0);
	let glowWidth = 0;
	let glowHeight = 0;
	let glowBlend: GlobalCompositeOperation = "source-over";
	let glowOpacity = 1;
	let glow: HTMLCanvasElement | null = null;

	function stamp(g: CanvasRenderingContext2D, sprite: HTMLCanvasElement, list: GlowStamp[]): void {
		const half = GLOW_PX / 2;
		for (const s of list) {
			const r = s.radius * half;
			g.globalAlpha = s.alpha;
			g.drawImage(sprite, half + s.x * half - r, half + s.y * half - r, r * 2, r * 2);
		}
	}

	/** The face-on light of the galaxy, painted once per theme. */
	function paintGlow(
		doc: Document,
		mode: GlobalCompositeOperation,
		core: ParsedColor,
		haze: ParsedColor,
		nebula: ParsedColor,
	): HTMLCanvasElement | null {
		const sprite = doc.createElement("canvas");
		sprite.width = GLOW_PX;
		sprite.height = GLOW_PX;
		const g = sprite.getContext("2d");
		const hazeDot = glowSprite(doc, 64, haze, 0.05);
		const laneDot = glowSprite(doc, 64, LANE_MASK, 0.35);
		const knotDot = glowSprite(doc, 48, nebula, 0.2);
		if (g === null || hazeDot === null || laneDot === null || knotDot === null) return null;
		const half = GLOW_PX / 2;
		g.globalCompositeOperation = mode;
		// A faint envelope gives the disc a body between the arms.
		const disc = g.createRadialGradient(half, half, 0, half, half, half);
		disc.addColorStop(0, cssColor(haze, 0.32));
		disc.addColorStop(0.45, cssColor(haze, 0.1));
		disc.addColorStop(1, cssColor(haze, 0));
		g.fillStyle = disc;
		g.fillRect(0, 0, GLOW_PX, GLOW_PX);
		stamp(g, hazeDot, armGlow(SEED + 1, 420));
		// The core sits behind the chat column at most window sizes, so it is
		// a soft glow, not a white-hot spot: at full strength body text over
		// it fell to under 3:1 in the dark theme.
		const bulge = g.createRadialGradient(half, half, 0, half, half, half * 0.5);
		bulge.addColorStop(0, cssColor(core, BULGE_PEAK));
		bulge.addColorStop(0.06, cssColor(core, BULGE_PEAK * 0.9));
		bulge.addColorStop(0.22, cssColor(core, BULGE_PEAK * 0.38));
		bulge.addColorStop(0.55, cssColor(core, BULGE_PEAK * 0.08));
		bulge.addColorStop(1, cssColor(core, 0));
		g.globalAlpha = 1;
		g.fillStyle = bulge;
		g.fillRect(0, 0, GLOW_PX, GLOW_PX);
		// Lanes are an absence of light, so they are cut out of what is there.
		g.globalCompositeOperation = "destination-out";
		stamp(g, laneDot, dustLanes(SEED + 2, 700));
		g.globalCompositeOperation = mode;
		stamp(g, knotDot, nebulaKnots(SEED + 3, 56));
		return sprite;
	}

	const light: Scene = {
		resize(w, h) {
			glowWidth = w;
			glowHeight = h;
			glowLayout = galaxyLayout(w, h);
		},
		recolor(read, doc) {
			glowBlend = blendMode(read("--bg-ambient-blend"));
			glowOpacity = tokenNumber(read, "--bg-galaxy-opacity", 1);
			glow = paintGlow(
				doc,
				glowBlend,
				tokenColor(read, "--bg-galaxy-core"),
				tokenColor(read, "--bg-galaxy-haze"),
				tokenColor(read, "--bg-galaxy-nebula"),
			);
		},
		draw(ctx, clockMs) {
			ctx.clearRect(0, 0, glowWidth, glowHeight);
			if (glow === null) return;
			const disc = glowLayout;
			ctx.globalCompositeOperation = glowBlend;
			ctx.globalAlpha = glowOpacity;
			ctx.translate(disc.cx, disc.cy);
			ctx.rotate(Math.atan2(disc.sinPa, disc.cosPa));
			ctx.scale(1, disc.tilt);
			ctx.rotate(spinAt(clockMs));
			ctx.drawImage(glow, -disc.radius, -disc.radius, disc.radius * 2, disc.radius * 2);
			ctx.globalAlpha = 1;
			ctx.globalCompositeOperation = "source-over";
		},
	};

	const starlight: Scene = {
		resize(w, h) {
			width = w;
			height = h;
			layout = galaxyLayout(w, h);
			stars = galaxyStars(SEED, starCount(layout.radius));
			sky = skyField({ seed: SEED + 9, width: w, height: h, per100k: 16, minR: 0.35, maxR: 1.05 });
		},
		recolor(read, doc) {
			blend = blendMode(read("--bg-ambient-blend"));
			opacity = tokenNumber(read, "--bg-galaxy-opacity", 1);
			toneFill = ["--bg-galaxy-core", "--bg-galaxy-arm", "--bg-galaxy-disc"].map((name) =>
				cssColor(tokenColor(read, name)),
			);
			skyFill = cssColor(tokenColor(read, "--bg-galaxy-sky"));
			halo = glowSprite(doc, 32, tokenColor(read, "--bg-galaxy-arm"), 0.12);
		},
		draw(ctx, clockMs) {
			ctx.clearRect(0, 0, width, height);
			ctx.globalCompositeOperation = blend;
			ctx.fillStyle = skyFill;
			for (const s of sky) {
				ctx.globalAlpha = twinkleAlpha(s, clockMs) * opacity;
				ctx.fillRect(s.x - s.r, s.y - s.r, s.r * 2, s.r * 2);
			}
			const spin = spinAt(clockMs);
			for (let tone = 0; tone < TONES; tone++) {
				ctx.fillStyle = toneFill[tone] ?? skyFill;
				for (let i = stars.toneStart[tone]; i < stars.toneStart[tone + 1]; i++) {
					projectInto(point, layout, stars.r[i], stars.angle[i], spin);
					const x = point[0];
					const y = point[1];
					if (x < -6 || y < -6 || x > width + 6 || y > height + 6) continue;
					const size = stars.size[i];
					ctx.globalAlpha = stars.alpha[i] * opacity;
					if (size >= HALO_FROM && halo !== null) {
						ctx.drawImage(halo, x - size * 2, y - size * 2, size * 4, size * 4);
					} else {
						ctx.fillRect(x - size / 2, y - size / 2, size, size);
					}
				}
			}
			ctx.globalAlpha = 1;
			ctx.globalCompositeOperation = "source-over";
		},
	};
</script>

<AmbientCanvas scene={light} {animate} {fill} resolution={0.5} fps={FPS} />
<AmbientCanvas scene={starlight} {animate} {fill} fps={FPS} />
