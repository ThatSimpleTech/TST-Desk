<script lang="ts">
	import { onMount } from 'svelte';
	import '../app.css';
	import favicon from '$lib/assets/favicon.svg';
	import { connect, disconnect } from '$lib/connection-status.svelte.js';

	let { children } = $props();

	onMount(() => {
		connect();
		return disconnect;
	});
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<main class="app-frame">
	{@render children()}
</main>

<style>
	.app-frame {
		display: flex;
		flex-direction: column;
		height: 100vh;
		overflow: hidden;
	}
</style>
