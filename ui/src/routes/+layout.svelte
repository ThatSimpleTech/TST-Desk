<script lang="ts">
	import '../app.css';
	import favicon from '$lib/assets/favicon.svg';

	let { children } = $props();
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
