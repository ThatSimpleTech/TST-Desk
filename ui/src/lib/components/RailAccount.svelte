<script lang="ts">
	// The rail's account anchor (TD-1712): pinned bottom-left rather than
	// buried behind a header gear. TST Desk has no accounts (§2) — the
	// identity shown is the daemon's active provider preset and whether a key
	// is stored, both from `setup_state`. The row opens TD-1703's settings
	// pane through its own store; it never reimplements it.
	import Icon from './Icon.svelte';
	import { settings, openSettings } from '../settings.svelte.js';
	import { accountRow } from '../rail';

	// `compact` is the rail's collapsed 48px strip: avatar only.
	let { compact = false }: { compact?: boolean } = $props();

	let account = $derived(accountRow(settings.activePreset, settings.hasApiKey));
</script>

<button
	class="account"
	class:account-compact={compact}
	type="button"
	title={`${account.label} · ${account.note} — open settings`}
	aria-label={`Account: ${account.label}, ${account.note}. Open settings`}
	onclick={() => openSettings()}
>
	<span class="avatar" aria-hidden="true">
		{#if account.initial === null}<Icon name="user" size={14} />{:else}{account.initial}{/if}
	</span>
	{#if !compact}
		<span class="account-text">
			<span class="account-label">{account.label}</span>
			<span class="account-note">{account.note}</span>
		</span>
		<span class="account-gear" aria-hidden="true"><Icon name="settings" size={14} /></span>
	{/if}
</button>

<style>
	/* The lists above own the rail's free space (flex: 1), so this row sits on
	   the floor in both modes without absolute positioning. */
	.account {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		text-align: left;
		flex-shrink: 0;
		border: none;
		border-top: var(--border-width) solid var(--color-hairline);
		background: transparent;
		padding: var(--space-2) var(--space-3);
		color: var(--color-ink);
		font-family: var(--font-sans);
		cursor: pointer;
	}

	.account:hover {
		background: var(--color-lifted);
	}

	.account-compact {
		width: auto;
		justify-content: center;
		border-top: none;
		padding: var(--space-1);
		border-radius: var(--radius-sm);
	}

	.avatar {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 24px;
		height: 24px;
		flex-shrink: 0;
		border-radius: var(--radius-full);
		background: var(--color-accent);
		color: var(--color-on-accent);
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		line-height: 1;
	}

	.account-text {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-width: 0;
		line-height: var(--leading-tight);
	}

	.account-label {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.account-note {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.account-gear {
		display: inline-flex;
		color: var(--color-ink-secondary);
		line-height: 1;
	}
</style>
