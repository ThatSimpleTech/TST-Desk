<script lang="ts">
	// The TST mascot on a new chat (TD-5190). It drops in, and on the
	// rebound its shackle pops open while it twirls once in the air; the
	// shackle snaps shut as it lands. Then it idles quietly: a blink, a
	// glance each way, a double blink, on a 12-second loop that starts
	// after the landing. Hovering lifts the shackle a pixel. With no live
	// session it sleeps: eyes shut, no entrance, no idle. Reduced motion
	// gets the still picture.
	//
	// The eye positions are separate sprite frames (see mascot.ts), swapped
	// with step timing so the eyes move in whole pixels like the art.
	import { MASCOT_HEIGHT, MASCOT_WIDTH, SHACKLE_PATH, bodyPath } from "../mascot";

	let { asleep = false }: { asleep?: boolean } = $props();

	const FRAMES = {
		ahead: bodyPath({ gaze: "ahead", shut: false }),
		left: bodyPath({ gaze: "left", shut: false }),
		right: bodyPath({ gaze: "right", shut: false }),
		shut: bodyPath({ gaze: "ahead", shut: true }),
	};

	// Three screen pixels per art pixel keeps every edge on the pixel grid.
	const SCALE = 3;
	// One row of headroom so the lifted shackle stays inside the picture.
	const HEADROOM = 1;
</script>

<span class="mascot" class:asleep data-testid="mascot" data-state={asleep ? "asleep" : "awake"} aria-hidden="true">
	<svg
		viewBox="0 {-HEADROOM} {MASCOT_WIDTH} {MASCOT_HEIGHT + HEADROOM}"
		width={MASCOT_WIDTH * SCALE}
		height={(MASCOT_HEIGHT + HEADROOM) * SCALE}
		shape-rendering="crispEdges"
	>
		<path class="shackle" d={SHACKLE_PATH} />
		{#if asleep}
			<path d={FRAMES.shut} />
		{:else}
			<path class="frame ahead" d={FRAMES.ahead} />
			<path class="frame left" d={FRAMES.left} />
			<path class="frame right" d={FRAMES.right} />
			<path class="frame shut" d={FRAMES.shut} />
		{/if}
	</svg>
</span>

<style>
	.mascot {
		display: inline-block;
		align-self: flex-start;
		line-height: 0;
		color: var(--color-ink);
		perspective: 240px;
		animation: drop 1300ms 120ms backwards;
	}

	svg {
		display: block;
		overflow: visible;
		fill: currentColor;
		animation: twirl 1300ms 120ms ease-in-out backwards;
	}

	.shackle {
		transform-box: fill-box;
		/* Swings open about the bottom of its left leg, like a real lock. */
		transform-origin: 10% 100%;
		animation: unlock 1300ms 120ms backwards;
	}

	.mascot:hover .shackle {
		transform: translateY(-1px);
	}

	.frame {
		opacity: 0;
		animation: 12s step-end 1500ms infinite;
	}

	.ahead {
		opacity: 1;
		animation-name: look-ahead;
	}

	.left {
		animation-name: look-left;
	}

	.right {
		animation-name: look-right;
	}

	.shut {
		animation-name: blink;
	}

	.asleep,
	.asleep svg,
	.asleep .shackle {
		animation: none;
	}

	@media (prefers-reduced-motion: reduce) {
		.mascot,
		svg,
		.shackle,
		.frame {
			animation: none;
		}
	}

	/* Fall in, hit, rebound high, land, one small hop, settle. */
	@keyframes drop {
		0% {
			opacity: 0;
			transform: translateY(-56px);
			animation-timing-function: ease-in;
		}
		8% {
			opacity: 1;
		}
		30% {
			transform: translateY(0);
			animation-timing-function: ease-out;
		}
		54% {
			transform: translateY(-30px);
			animation-timing-function: ease-in;
		}
		78% {
			transform: translateY(0);
			animation-timing-function: ease-out;
		}
		88% {
			transform: translateY(-5px);
			animation-timing-function: ease-in;
		}
		100% {
			transform: translateY(0);
		}
	}

	/* One full turn while airborne on the rebound. */
	@keyframes twirl {
		0%,
		30% {
			transform: rotateY(0deg);
		}
		78%,
		100% {
			transform: rotateY(360deg);
		}
	}

	/* Pops open on the first hit, stays open in the air, snaps shut on the
	   landing. */
	@keyframes unlock {
		0%,
		30% {
			transform: none;
			animation-timing-function: ease-out;
		}
		37%,
		76% {
			transform: translateY(-3px) rotate(-18deg);
			animation-timing-function: ease-in;
		}
		78%,
		100% {
			transform: none;
		}
	}

	/* Idle loop. Frames swap at the same instants, so exactly one shows. */
	@keyframes look-ahead {
		0% { opacity: 1; }
		22% { opacity: 0; }
		23.2% { opacity: 1; }
		45% { opacity: 0; }
		58% { opacity: 1; }
		62% { opacity: 0; }
		74% { opacity: 1; }
		88% { opacity: 0; }
		89.2% { opacity: 1; }
		90.4% { opacity: 0; }
		91.6% { opacity: 1; }
		100% { opacity: 1; }
	}

	@keyframes blink {
		0% { opacity: 0; }
		22% { opacity: 1; }
		23.2% { opacity: 0; }
		88% { opacity: 1; }
		89.2% { opacity: 0; }
		90.4% { opacity: 1; }
		91.6% { opacity: 0; }
		100% { opacity: 0; }
	}

	@keyframes look-left {
		0% { opacity: 0; }
		45% { opacity: 1; }
		58% { opacity: 0; }
		100% { opacity: 0; }
	}

	@keyframes look-right {
		0% { opacity: 0; }
		62% { opacity: 1; }
		74% { opacity: 0; }
		100% { opacity: 0; }
	}
</style>
