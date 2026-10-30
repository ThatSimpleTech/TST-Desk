<script lang="ts">
	// The one host for the picked background (TD-5202). It is mounted twice,
	// where the two Classic layers always were: `layer="window"` in the root
	// layout paints behind the whole app, and `layer="chat"` in the chat pane
	// adds what belongs to that pane — Classic's header light and dust, the
	// veil that quiets the other backgrounds behind the text, and the click
	// ripples. Switching swaps components in place: the old one's frame loop
	// and listeners go with it, and nothing reloads.
	//
	// Off renders nothing at all here, so it costs nothing: no canvas, no
	// listener, no frame.
	import { animates, ripplesLive } from "../../backgrounds/prefs";
	import { background } from "../../backgrounds/store.svelte.js";
	import ClassicChatLayer from "./ClassicChatLayer.svelte";
	import RippleLayer from "./RippleLayer.svelte";
	import { WINDOW_LAYERS } from "./registry";

	let { layer }: { layer: "window" | "chat" } = $props();

	const animate = $derived(animates(background));
	const ripples = $derived(ripplesLive(background));
	const WindowLayer = $derived(WINDOW_LAYERS[background.id]);
</script>

{#if layer === "window"}
	{#if WindowLayer}
		<WindowLayer {animate} fill="window" />
	{/if}
{:else}
	{#if background.id === "classic"}
		<ClassicChatLayer {animate} />
	{:else if background.id !== "off"}
		<div class="veil" aria-hidden="true"></div>
	{/if}
	{#if ripples}
		<RippleLayer />
	{/if}
{/if}

<style>
	/* A soft pool of ground colour behind the chat column: the background
	   stays lively at the edges and calm where the text is. */
	.veil {
		position: absolute;
		inset: 0;
		pointer-events: none;
		background: radial-gradient(ellipse 70% 62% at 50% 52%, var(--bg-veil), transparent 100%);
	}
</style>
