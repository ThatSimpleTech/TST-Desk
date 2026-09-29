<script lang="ts">
	// Settings → About (TD-4704, TD-4837). The version and the update
	// snapshot come from the host. Automatic checks are on until this
	// switch says otherwise.
	import pkg from '../../../package.json';
	import {
		checkForUpdates,
		openReleasePage,
		openUpdateInstaller,
		restartToUpdate,
		setAutomaticUpdates,
		update,
	} from '../update.svelte';

	let status = $derived(update.status);
	let automatic = $derived(status?.automatic ?? true);
	let busy = $derived(status?.phase === 'checking' || status?.phase === 'downloading');
	let version = $derived(status?.current ?? pkg.version);

	function checkLabel(): string {
		if (status?.phase === 'checking') return 'Checking…';
		if (status?.phase === 'downloading') return 'Downloading…';
		return 'Check for updates';
	}
</script>

<p class="version">TST Desk {version}</p>
<p class="hint">
	TST Desk looks for a newer GitHub release when a packaged app opens, and again every four
	hours while it stays open. The check sends no account and no identifier. A download is
	installed only when it matches <code>SHA256SUMS.txt</code> on that release. Builds are still
	unsigned — see <code>docs/signing.md</code>.
</p>

<div class="row">
	<div>
		<p class="title">Install updates automatically</p>
		<p class="hint">
			{automatic
				? 'On. A newer release downloads in the background. You restart to install it.'
				: 'Off. Nothing checks until you ask.'}
		</p>
	</div>
	<button
		class="choice"
		class:choice--active={automatic}
		type="button"
		role="switch"
		aria-checked={automatic}
		onclick={() => setAutomaticUpdates(!automatic)}>{automatic ? 'On' : 'Off'}</button
	>
</div>

<button class="choice" type="button" disabled={busy} onclick={checkForUpdates}>
	{checkLabel()}
</button>

{#if status?.detail}
	<p class="status" class:status--ok={status.phase === 'up_to_date'} class:status--error={status.phase === 'error'}>
		{status.detail}
		{#if status.latest}
			<span class="latest">Latest published: {status.latest}</span>
		{/if}
	</p>
{/if}

{#if status?.phase === 'ready' && status.can_apply}
	<button class="choice choice--active" type="button" onclick={restartToUpdate}>Restart to update</button>
{/if}
{#if status?.phase === 'ready' && status.can_open_installer}
	<button class="choice choice--active" type="button" onclick={openUpdateInstaller}>Open installer</button>
{/if}
{#if status?.release_url && status.phase !== 'up_to_date' && status.phase !== 'idle'}
	<button class="choice" type="button" onclick={openReleasePage}>Open releases page</button>
{/if}

<style>
	.version {
		font-size: var(--text-lg);
		font-weight: 600;
		margin: 0 0 var(--space-3);
	}
	.hint {
		color: var(--color-ink-muted);
		font-size: var(--text-sm);
		line-height: 1.5;
		margin: var(--space-1) 0 0;
	}
	.row {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-3);
		margin: var(--space-4) 0;
	}
	.title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		margin: 0;
	}
	.choice {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
		flex-shrink: 0;
	}
	.choice--active {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}
	.choice:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.status {
		margin: var(--space-3) 0 0;
		font-size: var(--text-sm);
		line-height: 1.5;
	}
	.status--ok {
		color: var(--color-ink-muted);
	}
	.status--error {
		color: var(--color-err);
	}
	.latest {
		display: block;
		margin-top: var(--space-2);
	}
	button.choice {
		margin-top: var(--space-3);
	}
	.row .choice {
		margin-top: 0;
	}
</style>
