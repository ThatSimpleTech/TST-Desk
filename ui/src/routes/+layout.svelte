<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import '../app.css';
	import favicon from '$lib/assets/favicon.svg';
	import BackgroundHost from '$lib/components/backgrounds/BackgroundHost.svelte';
	import { startBackground } from '$lib/backgrounds/store.svelte.js';
	import { connect, disconnect } from '$lib/connection-status.svelte.js';
	import { startMachines } from '$lib/machines.svelte.js';

	let { children } = $props();

	const isQuickEntry = $derived(page.url.pathname === '/quick-entry');

	onMount(() => {
		// The paired-computer list (TD-5015) follows the connection for the
		// window's whole life: the rail, the composer and Settings all read it.
		const offMachines = isQuickEntry ? () => {} : startMachines();
		// The picked background (TD-5202) follows the OS reduced-motion
		// setting and choices made in the app's other windows.
		const offBackground = startBackground();
		connect();
		return () => {
			offMachines();
			offBackground();
			disconnect();
		};
	});
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

{#if isQuickEntry}
	{@render children()}
{:else}
	<!-- The quick-entry card paints its own ground over the whole window,
	     so it gets no background layer to hide. -->
	<BackgroundHost layer="window" />
	<main class="app-frame">
		{@render children()}
	</main>
{/if}

<style>
	.app-frame {
		display: flex;
		flex-direction: column;
		height: 100vh;
		overflow: hidden;
	}
</style>
