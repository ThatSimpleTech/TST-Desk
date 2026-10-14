<script lang="ts">
	// Title-bar computer-use kill-switch (TD-3404). Presentational: the
	// latch is whatever `cu_kill_state` last said. Clicking sends
	// `set_cu_kill`; the daemon is the owner.
	//
	// TD-5015: the latch is this computer's. While the window follows a
	// session on another computer it is disabled, so it never reads as
	// stopping that computer's pointer.
	import { cuKill, setCuKill } from '../cu-kill.svelte.js';
	import { followingRemoteSession } from '../session-status.svelte.js';
	import Icon from './Icon.svelte';

	let remote = $derived(followingRemoteSession());
	let label = $derived(cuKill.killed ? 'Resume computer use' : 'Stop computer use');
	let title = $derived(
		remote
			? 'Stops computer use on this computer only. This session runs on another computer.'
			: cuKill.killed
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
	disabled={remote}
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
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		white-space: nowrap;
		transition: border-color var(--transition-fast), color var(--transition-fast),
			background var(--transition-fast);
	}

	.kill:hover:not(:disabled) {
		border-color: var(--color-err);
		color: var(--color-err);
	}

	.kill:disabled {
		cursor: default;
		color: var(--color-ink-muted);
	}

	.kill--engaged {
		color: var(--color-err);
		border-color: var(--color-err);
		background: color-mix(in srgb, var(--color-err) 12%, transparent);
	}

	.kill--engaged:hover:not(:disabled) {
		background: color-mix(in srgb, var(--color-err) 20%, transparent);
	}
</style>
