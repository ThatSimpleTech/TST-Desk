<script lang="ts">
	// The one host for the picked background (TD-5202). It is mounted twice,
	// where the two Classic layers always were: `layer="window"` in the root
	// layout paints behind the whole app, with the veil that quiets it behind
	// the text, and `layer="chat"` in the chat pane adds what belongs to that
	// pane — Classic's header light and dust, and the click ripples.
	// Switching swaps components in place: the old one's frame loop and
	// listeners go with it, and nothing reloads.
	//
	// Off renders nothing at all here, so it costs nothing: no canvas, no
	// listener, no frame.
	import { animates, ripplesLive } from "../../backgrounds/prefs";
	import { background } from "../../backgrounds/store.svelte.js";
	import ClassicChatLayer from "./ClassicChatLayer.svelte";
	import RippleLayer from "./RippleLayer.svelte";
	import { WINDOW_LAYERS, WINDOW_VEILS } from "./registry";

	let { layer }: { layer: "window" | "chat" } = $props();

	const animate = $derived(animates(background));
	const ripples = $derived(ripplesLive(background));
	const WindowLayer = $derived(WINDOW_LAYERS[background.id]);
	const veil = $derived(WINDOW_VEILS[background.id]);
</script>

{#if layer === "window"}
	{#if WindowLayer}
		<WindowLayer {animate} fill="window" />
	{/if}
	{#if veil}
		<div class="veil" style:background="var({veil})" aria-hidden="true"></div>
	{/if}
{:else}
	{#if background.id === "classic"}
		<ClassicChatLayer {animate} />
	{/if}
	{#if ripples}
		<RippleLayer />
	{/if}
{/if}

<style>
	/* The ground colour laid evenly over the background, under the app: it
	   comes after the background's layers, so at the same z-index it paints
	   on top of them. */
	.veil {
		position: fixed;
		inset: 0;
		z-index: -1;
		pointer-events: none;
	}
</style>
