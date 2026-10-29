<script lang="ts">
	// The mascot's body and eyes (TD-5190, TD-5192), drawn inside Mascot's
	// svg. The eyes are holes, so each eye position is its own body outline
	// (a sprite frame, see mascot.ts). While nothing holds the eyes, an idle
	// loop swaps frames with step timing: a blink, a glance each way, a
	// double blink, every 12 seconds. While Mascot holds a gaze (the cursor
	// or typing), that frame shows instead, blinking on its own clock.
	import { AHEAD, GAZES, bodyPath, type Gaze } from "../mascot";

	let {
		asleep,
		ready,
		arrived,
		look,
	}: {
		asleep: boolean;
		/** The entrance is running: look left on the walk, idle after it. */
		ready: boolean;
		/** The eyes have been held, so the idle loop runs without the walk. */
		arrived: boolean;
		look: Gaze | null;
	} = $props();

	const FRAMES = {
		ahead: bodyPath({ ...AHEAD, shut: false }),
		left: bodyPath({ dx: -1, dy: 0, shut: false }),
		right: bodyPath({ dx: 1, dy: 0, shut: false }),
		shut: bodyPath({ ...AHEAD, shut: true }),
	};
	const LOOKS = new Map(GAZES.map((g) => [`${g.dx},${g.dy}`, bodyPath({ ...g, shut: false })]));

	const BLINK_EVERY_MS = 4000;
	const BLINK_MS = 140;

	let blinking = $state(false);
	const looking = $derived(look !== null);

	// Keyed on whether a gaze is held, not which one, so a moving cursor
	// doesn't keep restarting the clock and the blinks still come.
	$effect(() => {
		if (!looking) return;
		let open: ReturnType<typeof setTimeout> | undefined;
		const timer = setInterval(() => {
			blinking = true;
			open = setTimeout(() => (blinking = false), BLINK_MS);
		}, BLINK_EVERY_MS);
		return () => {
			clearInterval(timer);
			clearTimeout(open);
			blinking = false;
		};
	});
</script>

<g class:ready class:arrived>
	{#if asleep}
		<path d={FRAMES.shut} />
	{:else if look !== null}
		<path class="held" d={blinking ? FRAMES.shut : LOOKS.get(`${look.dx},${look.dy}`)} />
	{:else}
		<path class="frame ahead" d={FRAMES.ahead} />
		<path class="frame left" d={FRAMES.left} />
		<path class="frame right" d={FRAMES.right} />
		<path class="frame shut" d={FRAMES.shut} />
	{/if}
</g>

<style>
	/* Timing comes from Mascot through inherited custom properties:
	   --start and --walk-ms for the walk, --idle-at for the landing. */
	.frame {
		opacity: 0;
	}

	.ahead {
		opacity: 1;
	}

	.ready .ahead {
		animation:
			look-ahead 12s step-end var(--idle-at) infinite,
			walk-eyes-ahead var(--walk-ms) step-end var(--start) backwards;
	}

	.ready .left {
		animation:
			look-left 12s step-end var(--idle-at) infinite,
			walk-eyes-left var(--walk-ms) step-end var(--start) backwards;
	}

	.ready .right {
		animation: look-right 12s step-end var(--idle-at) infinite;
	}

	.ready .shut {
		animation: blink 12s step-end var(--idle-at) infinite;
	}

	/* Back from a held gaze: the idle loop again, without the walk. */
	.ready.arrived .ahead {
		animation: look-ahead 12s step-end infinite;
	}

	.ready.arrived .left {
		animation: look-left 12s step-end infinite;
	}

	.ready.arrived .right {
		animation: look-right 12s step-end infinite;
	}

	.ready.arrived .shut {
		animation: blink 12s step-end infinite;
	}

	@media (prefers-reduced-motion: reduce) {
		.ready .frame,
		.ready.arrived .frame {
			animation: none;
		}
	}

	/* Eyes to the left while walking left, ahead on arrival. */
	@keyframes walk-eyes-left {
		0% { opacity: 1; }
		100% { opacity: 1; }
	}

	@keyframes walk-eyes-ahead {
		0% { opacity: 0; }
		100% { opacity: 0; }
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
