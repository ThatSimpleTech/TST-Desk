<script lang="ts">
	// Persistent update notice (TD-4837). Same two choices as Claude's
	// dialog — install, or later — without a modal over a running session.
	// Copy and actions come from the host snapshot.
	import {
		actionLabel,
		bannerTitle,
		bannerVisible,
		primaryAction,
	} from './update-status';
	import {
		checkForUpdates,
		dismissUpdate,
		downloadUpdate,
		openReleasePage,
		openUpdateInstaller,
		restartToUpdate,
		update,
	} from './update.svelte';

	let status = $derived(update.status);
	let visible = $derived(bannerVisible(status, update.dismissed));
	let action = $derived(status ? primaryAction(status) : null);

	async function runAction(): Promise<void> {
		if (action === 'download') await downloadUpdate();
		else if (action === 'restart') await restartToUpdate();
		else if (action === 'open') await openUpdateInstaller();
		else if (action === 'retry') await checkForUpdates();
		else if (action === 'releases') await openReleasePage();
	}
</script>

{#if visible && status}
	<div class="update" role="status">
		<div class="text">
			<span class="title">{bannerTitle(status)}</span>
			{#if status.detail}
				<span class="body">{status.detail}</span>
			{/if}
		</div>
		<div class="actions">
			{#if action}
				<button class="go" type="button" onclick={runAction}>{actionLabel(action)}</button>
			{/if}
			<button class="later" type="button" onclick={dismissUpdate}>Later</button>
		</div>
	</div>
{/if}

<style>
	.update {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-4);
		flex-shrink: 0;
		padding: var(--space-3) var(--space-6);
		background: var(--color-sunken);
		border-bottom: var(--border-width) solid var(--color-hairline);
		border-left: 3px solid var(--color-accent);
	}

	.text {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		min-width: 0;
	}

	.title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
	}

	.body {
		font-size: var(--text-sm);
		line-height: 1.45;
		color: var(--color-ink-muted);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
		flex-shrink: 0;
	}

	.go,
	.later {
		font-size: var(--text-sm);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.go {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border: 1px solid var(--color-accent);
	}

	.later {
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
	}
</style>
