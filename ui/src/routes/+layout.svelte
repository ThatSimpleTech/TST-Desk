<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import '../app.css';
	import favicon from '$lib/assets/favicon.svg';
	import { connect, disconnect } from '$lib/connection-status.svelte.js';

	let { children } = $props();

	const isQuickEntry = $derived(page.url.pathname === '/quick-entry');

	onMount(() => {
		connect();
		return disconnect;
	});
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

{#if isQuickEntry}
	{@render children()}
{:else}
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
