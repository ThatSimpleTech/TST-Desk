<script lang="ts">
	import { onMount } from 'svelte';
	import {
		bootQuickEntry,
		dismissQuickEntry,
		quickEntry,
		sendQuickEntry,
	} from '$lib/quick-entry.svelte.js';
	import { ws } from '$lib/connection-status.svelte.js';
	import { workspaceName } from '$lib/session-status.svelte.js';

	let textarea: HTMLTextAreaElement | null = $state(null);

	onMount(() => {
		let off = () => {};
		void bootQuickEntry().then((stop) => {
			off = stop;
		});
		return () => off();
	});

	$effect(() => {
		if (quickEntry.phase === 'ready' && textarea !== null) {
			textarea.focus();
		}
	});

	function handleKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') {
			event.preventDefault();
			dismissQuickEntry();
			return;
		}
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			sendQuickEntry();
		}
	}
</script>

<div class="overlay" role="presentation">
	<div class="card" role="dialog" aria-label="Quick entry">
		{#if quickEntry.phase === 'connecting' || ws.state !== 'connected'}
			<p class="hint">Connecting…</p>
		{:else if quickEntry.phase === 'error'}
			<p class="error">{quickEntry.error ?? 'Quick entry is unavailable.'}</p>
			<button class="btn" type="button" onclick={() => dismissQuickEntry()}>Dismiss</button>
		{:else}
			<p class="target">
				{quickEntry.workspacePath !== null
					? workspaceName(quickEntry.workspacePath)
					: 'Workspace'}
			</p>
			<textarea
				bind:this={textarea}
				bind:value={quickEntry.draft}
				rows="2"
				placeholder="Message the agent…"
				aria-label="Quick entry message"
				onkeydown={handleKeydown}
			></textarea>
			<p class="hint">Enter to send · Esc to dismiss · ⌘⇧.</p>
		{/if}
	</div>
</div>

<style>
	.overlay {
		display: flex;
		align-items: stretch;
		justify-content: stretch;
		min-height: 100vh;
		background: var(--color-ground);
		padding: var(--space-3);
	}

	.card {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		flex: 1;
		background: var(--color-lifted);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-md);
		padding: var(--space-3);
	}

	.target {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: 0;
	}

	textarea {
		flex: 1;
		min-height: 3rem;
		resize: none;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: var(--space-2) var(--space-3);
		font: inherit;
		color: var(--color-ink);
		background: var(--color-lifted);
	}

	textarea:focus {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
	}

	.hint {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-muted);
	}

	.error {
		margin: 0;
		color: var(--color-err);
		font-size: var(--text-sm);
	}

	.btn {
		align-self: flex-start;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-3);
		background: var(--color-lifted);
		cursor: pointer;
	}
</style>
