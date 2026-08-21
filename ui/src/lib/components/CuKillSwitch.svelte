<script lang="ts">
	// Title-bar computer-use kill-switch (TD-3404). Presentational: the
	// latch is whatever `cu_kill_state` last said. Clicking sends
	// `set_cu_kill`; the daemon is the owner.
	import { cuKill, setCuKill } from '../cu-kill.svelte.js';
	import Icon from './Icon.svelte';

	let label = $derived(cuKill.killed ? 'Resume computer use' : 'Stop computer use');
	let title = $derived(
		cuKill.killed
			? 'Computer use is stopped. Capture still works. Click to resume.'
			: 'Stop all computer-use actuation immediately (⌘.)',
	);
</script>

<button
	class="kill"
	class:kill--engaged={cuKill.killed}
	type="button"
	aria-pressed={cuKill.killed}
	aria-label={label}
	{title}
	onclick={() => setCuKill(!cuKill.killed)}
>
	<Icon name="stop" size={12} />
	{cuKill.killed ? 'CU stopped' : 'Stop CU'}
</button>

<style>
	.kill {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text-secondary);
		background: transparent;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		white-space: nowrap;
		transition: border-color var(--transition-fast), color var(--transition-fast),
			background var(--transition-fast);
	}

	.kill:hover {
		border-color: var(--color-danger);
		color: var(--color-danger);
	}

	.kill--engaged {
		color: var(--color-danger);
		border-color: var(--color-danger);
		background: color-mix(in srgb, var(--color-danger) 12%, transparent);
	}

	.kill--engaged:hover {
		background: color-mix(in srgb, var(--color-danger) 20%, transparent);
	}
</style>
