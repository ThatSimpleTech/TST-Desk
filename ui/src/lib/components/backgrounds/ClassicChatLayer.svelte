<script lang="ts">
	// Classic's layers inside the chat pane (TD-5202 gathered them here from
	// ChatPane, unchanged): the slow light under the header — two soft bands
	// drift, the Claude Code top-gradient idea — and the TD-1750 dust. The
	// orbit halo behind the whole window is Classic's other half.
	import PlayfulBackground from "../PlayfulBackground.svelte";

	let { animate }: { animate: boolean } = $props();
</script>

<div class="ambient" class:ambient--still={!animate} aria-hidden="true">
	<span class="band"></span>
	<span class="band band--warm"></span>
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

	.band {
		position: absolute;
		top: -80px;
		left: 8%;
		width: 46%;
		height: 200px;
		border-radius: 50%;
		background: color-mix(in srgb, var(--color-accent) 72%, transparent);
		filter: blur(48px);
		opacity: 0.42;
		animation: drift 18s var(--ease-out) infinite alternate;
	}

	.band--warm {
		left: auto;
		right: 4%;
		width: 38%;
		background: color-mix(in srgb, var(--color-warn) 70%, transparent);
		opacity: 0.32;
		animation-duration: 26s;
		animation-direction: alternate-reverse;
	}

	/* Motion off, or the OS asking for less motion, holds the bands still. */
	.ambient--still .band {
		animation: none;
	}

	@keyframes drift {
		from {
			transform: translate3d(0, 0, 0);
		}
		to {
			transform: translate3d(10%, 12px, 0);
		}
	}
</style>
