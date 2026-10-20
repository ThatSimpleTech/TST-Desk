<script lang="ts">
	// The native loop's task list as the daemon sent it (TD-5122): one row
	// per item, its state as a word and a mark, so it reads without colour.
	// Shared by the Plan pane and the chat's task strip.
	import type { TaskListItem } from '../protocol';
	import { statusLabel } from '../task-list';

	let { items }: { items: readonly TaskListItem[] } = $props();
</script>

<ol class="tasks" aria-label="Tasks">
	{#each items as item, i (i)}
		<li class="task" data-status={item.status}>
			<span class="mark" aria-hidden="true"
				>{item.status === 'done' ? '✓' : item.status === 'in_progress' ? '▸' : '○'}</span
			>
			<span class="content">{item.content}</span>
			<span class="status">{statusLabel(item.status)}</span>
		</li>
	{/each}
</ol>

<style>
	.tasks {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		font-size: var(--text-sm);
	}
	.task {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		min-width: 0;
	}
	.mark {
		flex: none;
		width: 1em;
		text-align: center;
		color: var(--color-ink-muted);
	}
	.task[data-status='in_progress'] .mark {
		color: var(--color-accent);
	}
	.content {
		flex: 1;
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.task[data-status='done'] .content {
		color: var(--color-ink-muted);
		text-decoration: line-through;
	}
	.task[data-status='in_progress'] .content {
		font-weight: 600;
	}
	.status {
		flex: none;
		color: var(--color-ink-muted);
		font-size: 0.75rem;
		text-transform: uppercase;
	}
</style>
