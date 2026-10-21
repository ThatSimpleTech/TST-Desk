<script lang="ts">
	// Hooks inspector (TD-5134): the attached session's active hooks, from
	// user config and (once trusted) the workspace's, each with its last
	// result. Display only; hooks are edited in config.yaml. Subscribe and
	// refresh live in AppShell, as for the Stack panel.
	import EmptyState from './EmptyState.svelte';
	import { hooksView } from '../hooks-store.svelte.js';
	import { hookEventLabel, lastRunLabel, workspaceNote } from '../hooks-store';

	const note = $derived(workspaceNote(hooksView));
</script>

<div class="hooks-panel">
	{#if note}
		<p class="note" role="status">{note}</p>
	{/if}
	{#if !hooksView.loaded}
		<EmptyState icon="terminal" body="No hooks loaded yet." />
	{:else if hooksView.hooks.length === 0}
		<EmptyState icon="terminal" body="No hooks. Add them under hooks: in config.yaml." />
	{:else}
		<ul class="hooks" aria-label="Active hooks">
			{#each hooksView.hooks as hook (hook.id)}
				<li class="hook">
					<div class="head">
						<span class="event">{hookEventLabel(hook.event)}</span>
						<span class="chip">{hook.source}</span>
						{#if hook.tools && hook.tools.length > 0}
							<span class="chip">{hook.tools.join(', ')}</span>
						{/if}
					</div>
					<code class="command" title={hook.command}>{hook.name ?? hook.command}</code>
					<div
						class="last"
						class:last-block={hook.last?.result === 'block' || hook.last?.result === 'ask'}
						class:last-bad={hook.last?.result === 'error' || hook.last?.result === 'timeout'}
					>
						{lastRunLabel(hook.last)}
						{#if hook.last?.reason}
							<span class="reason">— {hook.last.reason}</span>
						{/if}
					</div>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.hooks-panel {
		height: 100%;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
	}

	.note {
		margin: 0;
		padding: var(--space-2) var(--space-4);
		font-size: var(--text-xs);
		color: var(--color-warn);
		border-bottom: var(--border-width) solid var(--color-hairline);
	}

	.hooks {
		list-style: none;
		margin: 0;
		padding: var(--space-2) 0;
	}

	.hook {
		padding: var(--space-2) var(--space-4);
		border-bottom: var(--border-width) solid var(--color-hairline);
	}

	.head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}

	.chip {
		padding: 0 var(--space-2);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		font-size: var(--text-xs);
		font-weight: var(--weight-normal);
		color: var(--color-ink-muted);
	}

	.command {
		display: block;
		margin-top: var(--space-1);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.last {
		margin-top: var(--space-1);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.last-block {
		color: var(--color-warn);
	}

	.last-bad {
		color: var(--color-err);
	}

	.reason {
		overflow-wrap: anywhere;
	}
</style>
