<script lang="ts">
	// The TST mascot on a new chat (TD-5190). It waddles in from the right
	// in short hops, rocking side to side and looking where it is going
	// (TD-5191). At its spot it jumps: the shackle unhooks on the left and
	// spins a full turn about its right leg, the way a real padlock's
	// shackle swivels, while the body stays upright. The shackle drops back
	// in and snaps shut as the body lands. Then it idles (MascotEyes), and
	// its eyes follow the cursor over the window and glance at whatever is
	// being typed into (TD-5192). Hovering lifts the shackle a pixel. With
	// no live session it sleeps: eyes shut, no entrance, no idle. Reduced
	// motion gets the still picture, though the eyes still follow: they move
	// only when the cursor or typing does.
	import { EYE_ROW, MASCOT_HEIGHT, MASCOT_WIDTH, SHACKLE_PATH, type Gaze } from "../mascot";
	import { watchGaze } from "../mascot-gaze";
	import MascotEyes from "./MascotEyes.svelte";

	let { asleep = false }: { asleep?: boolean } = $props();

	// Three screen pixels per art pixel keeps every edge on the pixel grid.
	const SCALE = 3;
	// One row of headroom so the lifted shackle stays inside the picture.
	const HEADROOM = 1;

	const START_MS = 120;
	const HOP_MS = 220;
	const JUMP_MS = 1100;
	// About a body's width per hop, so it waddles rather than strides, and
	// never more than 480px of walking however wide the window is.
	const STRIDE_PX = 64;
	const MAX_WALK_PX = 480;

	let el: HTMLSpanElement | undefined = $state();
	let svg: SVGSVGElement | undefined = $state();
	let walk = $state<{ px: number; hops: number } | null>(null);
	let landed = $state(false);
	// Where the eyes are held, or null while the idle loop has them.
	let look = $state<Gaze | null>(null);
	let arrived = $state(false);

	// The walk starts at the right of whatever holds the mascot, so measure
	// that once on arrival. Hops come in pairs: one rock each way.
	$effect(() => {
		if (el === undefined || asleep) return;
		const parent = el.parentElement;
		let room = 0;
		if (parent !== null) {
			const style = getComputedStyle(parent);
			const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
			room = parent.clientWidth - (Number.isNaN(padding) ? 0 : padding) - el.offsetWidth;
		}
		const px = Math.max(0, Math.min(MAX_WALK_PX, room));
		walk = { px, hops: px === 0 ? 0 : Math.max(2, 2 * Math.round(px / STRIDE_PX / 2)) };
	});

	const walkMs = $derived((walk?.hops ?? 0) * HOP_MS);

	// The eyes are free once it has landed: straight away without motion.
	$effect(() => {
		if (walk === null || asleep) return;
		const still = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;
		const timer = setTimeout(() => (landed = true), still ? 0 : START_MS + walkMs + JUMP_MS);
		return () => clearTimeout(timer);
	});

	$effect(() => {
		if (!landed || asleep) return;
		return watchGaze(window, {
			eyes: () => {
				if (svg === undefined) return null;
				const box = svg.getBoundingClientRect();
				const row = (EYE_ROW + HEADROOM) / (MASCOT_HEIGHT + HEADROOM);
				return { x: box.left + box.width / 2, y: box.top + box.height * row };
			},
			onLook: (gaze) => {
				if (gaze !== null) arrived = true;
				look = gaze;
			},
		});
	});
</script>

<span
	bind:this={el}
	class="mascot"
	class:ready={walk !== null}
	data-testid="mascot"
	data-state={asleep ? "asleep" : "awake"}
	data-walk-ms={walkMs}
	aria-hidden="true"
	style:--walk="{walk?.px ?? 0}px"
	style:--hops={walk?.hops ?? 0}
	style:--waddles={(walk?.hops ?? 0) / 2}
	style:--start="{START_MS}ms"
	style:--hop-ms="{HOP_MS}ms"
	style:--walk-ms="{walkMs}ms"
	style:--jump-ms="{JUMP_MS}ms"
	style:--jump-at="{START_MS + walkMs}ms"
	style:--idle-at="{START_MS + walkMs + JUMP_MS + 200}ms"
>
	<span class="hop">
		<svg
			bind:this={svg}
			viewBox="0 {-HEADROOM} {MASCOT_WIDTH} {MASCOT_HEIGHT + HEADROOM}"
			width={MASCOT_WIDTH * SCALE}
			height={(MASCOT_HEIGHT + HEADROOM) * SCALE}
			shape-rendering="crispEdges"
		>
			<path class="shackle" d={SHACKLE_PATH} />
			<MascotEyes {asleep} ready={walk !== null} {arrived} {look} />
		</svg>
	</span>
</span>

<style>
	.mascot {
		display: inline-block;
		align-self: flex-start;
		line-height: 0;
		color: var(--color-ink);
	}

	.hop {
		display: block;
	}

	svg {
		display: block;
		/* The spinning shackle reaches past the body's right edge. */
		overflow: visible;
		fill: currentColor;
		/* Rocks on its base, like a step from one foot to the other. */
		transform-origin: 50% 100%;
	}

	.shackle {
		transform-box: fill-box;
		/* The hinge: the bottom of the right leg, the one that stays put.
		   That leg's centre is 14.5 of the shackle's 16 pixels across. */
		transform-origin: 90.625% 100%;
	}

	.mascot:hover .shackle {
		transform: translateY(-1px);
	}

	/* Everything moves only once the walk is measured, and only one
	   animation drives each property at a time: the walk ends where the
	   jump begins, and the idle loop waits for the landing. */
	.ready {
		animation: travel var(--walk-ms) linear var(--start) backwards;
	}

	.ready .hop {
		animation:
			hop var(--hop-ms) var(--start) var(--hops),
			jump var(--jump-ms) var(--jump-at);
	}

	.ready svg {
		animation: waddle calc(var(--hop-ms) * 2) ease-in-out var(--start) var(--waddles);
	}

	.ready .shackle {
		animation: unlock var(--jump-ms) var(--jump-at);
	}

	@media (prefers-reduced-motion: reduce) {
		.ready,
		.ready .hop,
		.ready svg,
		.ready .shackle {
			animation: none;
		}
	}

	/* In from the right, fading up over the first steps. */
	@keyframes travel {
		0% {
			opacity: 0;
			transform: translateX(var(--walk));
		}
		12% {
			opacity: 1;
		}
		100% {
			transform: translateX(0);
		}
	}

	@keyframes hop {
		0% {
			transform: translateY(0);
			animation-timing-function: ease-out;
		}
		50% {
			transform: translateY(-8px);
			animation-timing-function: ease-in;
		}
		100% {
			transform: translateY(0);
		}
	}

	/* One rock each way per pair of hops. */
	@keyframes waddle {
		0%,
		50%,
		100% {
			transform: rotate(0deg);
		}
		25% {
			transform: rotate(-9deg);
		}
		75% {
			transform: rotate(9deg);
		}
	}

	/* Up high, land, one small hop, settle. */
	@keyframes jump {
		0% {
			transform: translateY(0);
			animation-timing-function: ease-out;
		}
		36% {
			transform: translateY(-30px);
			animation-timing-function: ease-in;
		}
		73% {
			transform: translateY(0);
			animation-timing-function: ease-out;
		}
		86% {
			transform: translateY(-5px);
			animation-timing-function: ease-in;
		}
		100% {
			transform: translateY(0);
		}
	}

	/* Unhook on take-off: the left leg lifts out first. Level off, spin a
	   full turn about the right leg (scaleX through -1 draws the far side
	   of the turn, with no 3D, so WebKit and Chromium agree), then drop
	   back in as the body lands. Every keyframe lists the same functions
	   so each step interpolates. */
	@keyframes unlock {
		0% {
			transform: translateY(0) rotate(0deg) scaleX(1);
			animation-timing-function: ease-out;
		}
		8% {
			transform: translateY(-2px) rotate(12deg) scaleX(1);
			animation-timing-function: ease-in-out;
		}
		15% {
			transform: translateY(-3px) rotate(0deg) scaleX(1);
			animation-timing-function: ease-in;
		}
		27.5% {
			transform: translateY(-3px) rotate(0deg) scaleX(0);
			animation-timing-function: ease-out;
		}
		40% {
			transform: translateY(-3px) rotate(0deg) scaleX(-1);
			animation-timing-function: ease-in;
		}
		52.5% {
			transform: translateY(-3px) rotate(0deg) scaleX(0);
			animation-timing-function: ease-out;
		}
		65% {
			transform: translateY(-3px) rotate(0deg) scaleX(1);
		}
		70% {
			transform: translateY(-3px) rotate(0deg) scaleX(1);
			animation-timing-function: ease-in;
		}
		73%,
		100% {
			transform: translateY(0) rotate(0deg) scaleX(1);
		}
	}
</style>
