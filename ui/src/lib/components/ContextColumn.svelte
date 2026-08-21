<script lang="ts">
	// Context column on the project home (TD-2804).
	//
	// Pins are workspace files/folders. + is a picker inside the wall.
	// Search filters the cards. Unpin removes the pin, not the file.
	import Icon from './Icon.svelte';
	import { contextEmptyCopy, filterPins, pinKindLabel } from '../context-pins';
	import {
		addContextPin,
		contextPins,
		loadContextPins,
		removeContextPin,
		setPinQuery,
		startContextPins,
	} from '../context-pins.svelte.js';
	import { isTauri } from '../open-file';

	let { workspacePath }: { workspacePath: string } = $props();

	startContextPins();

	$effect(() => {
		loadContextPins(workspacePath);
	});

	let visible = $derived(filterPins(contextPins.pins, contextPins.query));

	async function pick(directory: boolean): Promise<void> {
		if (!isTauri()) return;
		const { open } = await import('@tauri-apps/plugin-dialog');
		const chosen = await open({
			defaultPath: workspacePath,
			directory,
			multiple: false,
		});
		if (typeof chosen === 'string') addContextPin(chosen);
	}
</script>

<section class="col" aria-label="Context">
	<div class="head">
		<h2 class="section">Context</h2>
		<div class="plus">
			<button class="icon-btn" type="button" aria-label="Pin a file" title="Pin a file" onclick={() => void pick(false)}>
				<Icon name="plus" size={14} />
			</button>
			<button class="text-btn" type="button" onclick={() => void pick(true)}>Folder</button>
		</div>
	</div>
	<input
		class="search"
		type="search"
		placeholder="Search pins"
		aria-label="Search pins"
		value={contextPins.query}
		oninput={(e) => setPinQuery(e.currentTarget.value)}
	/>
	{#if contextPins.pins.length === 0}
		<p class="empty">{contextEmptyCopy()}</p>
	{:else if visible.length === 0}
		<p class="empty">No pins match.</p>
	{:else}
		<ul class="list">
			{#each visible as pin (pin.path)}
				<li class="card">
					<div class="card-text">
						<span class="name">{pin.name}</span>
						<span class="meta">{pinKindLabel(pin.kind)} · {pin.lines} lines</span>
					</div>
					<button class="text-btn" type="button" onclick={() => removeContextPin(pin.path)}>Unpin</button>
				</li>
			{/each}
		</ul>
	{/if}
	{#if contextPins.capacityCap > 0}
		<p class="meter" aria-label="Project capacity">
			{contextPins.instructionTokens + contextPins.memoryTokens + contextPins.pinTokens}
			/ {contextPins.capacityCap} tokens
			{#if contextPins.dropped.length > 0}
				· dropped last-in-first-out: {contextPins.dropped.join(', ')}
			{/if}
		</p>
	{/if}
	{#if contextPins.error !== null}
		<p class="err">{contextPins.error}</p>
	{/if}
</section>

<style>
	.col {
		min-width: 0;
	}

	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
	}

	.section {
		margin: 0;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--color-ink-muted);
	}

	.plus {
		display: flex;
		align-items: center;
		gap: var(--space-1);
	}

	.icon-btn {
		border: none;
		background: transparent;
		color: var(--color-accent);
		cursor: pointer;
		padding: var(--space-1);
	}

	.search {
		margin-top: var(--space-3);
		width: 100%;
		box-sizing: border-box;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-lifted);
		color: var(--color-ink);
		padding: var(--space-2) var(--space-3);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
	}

	.empty {
		margin: var(--space-3) 0 0;
		font-size: var(--text-sm);
		color: var(--color-ink-muted);
	}

	.list {
		list-style: none;
		margin: var(--space-3) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.card {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		border-radius: var(--radius-md);
		padding: var(--space-2) var(--space-3);
	}

	.card-text {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}

	.name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
	}

	.meta {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.text-btn {
		border: none;
		background: transparent;
		color: var(--color-accent);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		cursor: pointer;
		padding: var(--space-1);
	}

	.meter {
		margin: var(--space-3) 0 0;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.err {
		margin: var(--space-2) 0 0;
		font-size: var(--text-xs);
		color: var(--color-err);
	}
</style>
