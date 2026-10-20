<script lang="ts">
	// The native loop's task list above the composer (TD-5122). On a phone
	// the chat is the one pane on screen, so this is where the list shows
	// there; on a wide window the Plan pane has it as well. One line until
	// opened: how far along, and the item in progress. Renders nothing once
	// every item is done; the Plan pane still has the finished list.
	import type { TaskListItem } from '../../protocol';
	import { currentTask, progressLabel, showTaskStrip } from '../../task-list';
	import Icon from '../Icon.svelte';
	import TaskListView from '../TaskListView.svelte';

	let { items }: { items: readonly TaskListItem[] } = $props();

	let open = $state(false);
	let current = $derived(currentTask(items));
	const uid = $props.id();
	let summaryId = $derived(`task-strip-${uid}`);
</script>

{#if showTaskStrip(items)}
	<section class="strip" aria-label="Task progress">
		<button
			class="head"
			type="button"
			aria-label="Task list"
			aria-describedby={summaryId}
			aria-expanded={open}
			onclick={() => (open = !open)}
		>
			<span class="summary" id={summaryId}>
				<span class="progress">Tasks · {progressLabel(items)}</span>
				{#if current !== null && !open}
					<span class="current">{current.content}</span>
				{/if}
			</span>
			<span class="caret" class:caret-open={open} aria-hidden="true">
				<Icon name="chevron-down" size={14} />
			</span>
		</button>
		{#if open}
			<div class="body">
				<TaskListView {items} />
			</div>
		{/if}
	</section>
{/if}

<style>
	.strip {
		margin: 0 var(--space-4) var(--space-2);
		background: var(--color-sunken);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-lg);
	}

	.head {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		padding: var(--space-1) var(--space-2) var(--space-1) var(--space-3);
		font: inherit;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: none;
		border-radius: var(--radius-lg);
		cursor: pointer;
		text-align: left;
	}

	.head:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
	}

	.summary {
		display: flex;
		flex: 1;
		gap: var(--space-2);
		min-width: 0;
	}

	.progress {
		flex: none;
		color: var(--color-ink-muted);
	}

	.current {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--color-ink);
	}

	.caret {
		display: inline-flex;
		flex: none;
		color: var(--color-ink-muted);
		transition: transform var(--transition-fast);
	}

	.caret-open {
		transform: rotate(180deg);
	}

	.body {
		max-height: calc(var(--space-16) * 3);
		overflow-y: auto;
		padding: 0 var(--space-3) var(--space-2);
	}
</style>
