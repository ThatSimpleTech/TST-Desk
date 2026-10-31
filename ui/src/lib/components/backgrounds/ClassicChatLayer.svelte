<script lang="ts">
	// Classic's layers inside the chat pane (TD-5202 gathered them here from
	// ChatPane): the slow light under the header — two soft bands drift, the
	// Claude Code top-gradient idea — and the TD-1750 dust. The orbit halo
	// behind the whole window is Classic's other half. All of it steps on
	// Classic's one shared tick (backgrounds/classic.ts), so a tick repaints
	// the window once, not once per layer.
	//
	// Each band keeps its old box and drift; its blur is painted once into a
	// canvas it carries (backgrounds/band-glow.ts), not worked out again on
	// every repaint as filter: blur(48px) was. The canvas is half the pixel
	// ratio: the band has no detail finer than its 48 px blur.
	import AmbientCanvas from "./AmbientCanvas.svelte";
	import PlayfulBackground from "../PlayfulBackground.svelte";
	import { BAND_PAD, bandGlowScene } from "../../backgrounds/band-glow";
	import { bandDrift, classicTick, type BandDrift } from "../../backgrounds/classic";

	let { animate }: { animate: boolean } = $props();

	/** The old bands: 72% of the accent at 0.42 opacity, and 70% of the warn
	 *  colour at 0.32. */
	const accentGlow = bandGlowScene("--color-accent", 0.72 * 0.42);
	const warmGlow = bandGlowScene("--color-warn", 0.7 * 0.32);
	const reach = `-${BAND_PAD}px`;

	let drift: BandDrift | null = $state.raw(null);

	$effect(() => {
		if (!animate) {
			drift = null;
			return;
		}
		const frames = classicTick.join((clockMs) => {
			drift = bandDrift(clockMs);
		});
		frames.paintNow();
		frames.run(true);
		return () => frames.dispose();
	});
</script>

<div class="ambient" aria-hidden="true">
	<span class="band" style:--drift={drift?.accent}>
		<span class="glow" style:inset={reach}>
			<AmbientCanvas scene={accentGlow} animate={false} fill="parent" resolution={0.5} />
		</span>
	</span>
	<span class="band band--warm" style:--drift={drift?.warm}>
		<span class="glow" style:inset={reach}>
			<AmbientCanvas scene={warmGlow} animate={false} fill="parent" resolution={0.5} />
		</span>
	</span>
</div>
<PlayfulBackground {animate} />

<style>
	.ambient {
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 280px;
		pointer-events: none;
		overflow: hidden;
	}

	/* --drift comes from Classic's tick: 0 at home, 1 at a tenth of the band's
	   width to the right and 12 px down, as the old keyframes moved it. */
	.band {
		position: absolute;
		top: -80px;
		left: 8%;
		width: 46%;
		height: 200px;
		transform: translate3d(calc(var(--drift, 0) * 10%), calc(var(--drift, 0) * 12px), 0);
	}

	.band--warm {
		left: auto;
		right: 4%;
		width: 38%;
	}

	.glow {
		position: absolute;
	}
</style>
