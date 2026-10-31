<script lang="ts">
	// An MCP server's env or headers (TD-5203): names only, and where each
	// value lives. The daemon never sends a value here, and this view never
	// writes one; its own page keeps a token in the keychain.
	import type { JsonValue } from '../../protocol';

	let { id, value }: { id: string; value: JsonValue | undefined } = $props();

	const names = $derived(
		value !== null && typeof value === 'object' && !Array.isArray(value)
			? Object.entries(value).map(([name, source]) => ({ name, source: String(source) }))
			: [],
	);
</script>

<ul {id} class="cfg-rows">
	{#each names as row (row.name)}
		<li class="cfg-secret">
			<span class="cfg-key">{row.name}</span>
			<span class="cfg-muted">{row.source === 'keychain' ? 'in the keychain' : 'set in config.yaml'}</span>
		</li>
	{:else}
		<li class="cfg-muted">None.</li>
	{/each}
</ul>
