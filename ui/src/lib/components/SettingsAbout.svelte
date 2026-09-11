<script lang="ts">
	// Settings → About (TD-4704): version + opt-in release check.
	// No background checks; in-app install stays off until signing lands.
	import { invoke } from '@tauri-apps/api/core';
	import { openUrl } from '@tauri-apps/plugin-opener';
	import pkg from '../../../package.json';

	type UpdateCheckResult = {
		current: string;
		latest: string | null;
		update_available: boolean;
		in_app_install_enabled: boolean;
		release_url: string | null;
		detail: string;
	};

	let checking = $state(false);
	let result = $state<UpdateCheckResult | null>(null);
	let error = $state<string | null>(null);

	async function checkForUpdates(): Promise<void> {
		checking = true;
		error = null;
		try {
			result = await invoke<UpdateCheckResult>('check_for_updates');
		} catch (e) {
			result = null;
			error = e instanceof Error ? e.message : String(e);
		} finally {
			checking = false;
		}
	}

	async function openRelease(): Promise<void> {
		const url = result?.release_url;
		if (!url) return;
		await openUrl(url);
	}
</script>

<p class="version">TST Desk {pkg.version}</p>
<p class="hint">
	Updates are opt-in only — nothing checks in the background. v0.1 ships unsigned;
	in-app install stays off until signed releases exist (see <code>docs/signing.md</code>).
</p>

<button class="choice" type="button" disabled={checking} onclick={checkForUpdates}>
	{checking ? 'Checking…' : 'Check for updates'}
</button>

{#if error}
	<p class="status status--error" role="alert">{error}</p>
{/if}

{#if result}
	<p class="status" class:status--ok={!result.update_available}>
		{result.detail}
		{#if result.latest}
			<span class="latest">Latest published: {result.latest}</span>
		{/if}
	</p>
	{#if result.update_available && result.release_url}
		<button class="choice" type="button" onclick={openRelease}>Open releases page</button>
	{/if}
{/if}

<style>
	.version {
		font-size: var(--text-lg);
		font-weight: 600;
		margin: 0 0 var(--space-3);
	}
	.hint {
		color: var(--text-muted);
		font-size: var(--text-sm);
		line-height: 1.5;
		margin: 0 0 var(--space-4);
	}
	.status {
		margin: var(--space-3) 0 0;
		font-size: var(--text-sm);
		line-height: 1.5;
	}
	.status--ok {
		color: var(--text-muted);
	}
	.status--error {
		color: var(--danger);
	}
	.latest {
		display: block;
		margin-top: var(--space-2);
	}
</style>
