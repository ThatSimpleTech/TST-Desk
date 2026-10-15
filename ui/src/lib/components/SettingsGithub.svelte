<script lang="ts">
	// GitHub device flow (TD-4852). The user code is shown here. The device
	// code and the tokens never arrive. The browser opens only when asked,
	// and only for an https URL with no userinfo.
	import { connectGithub, disconnectGithub, settings } from '../settings.svelte.js';

	const OFF =
		"GitHub is off until config.yaml sets connectors.github.client_id. Create a GitHub App, turn on Device Flow (no client secret, no callback URL), and paste that App's client id. The hosts are connectors.github.api_host and connectors.github.web_host.";

	function httpsUrl(raw: string): string | null {
		let url: URL;
		try {
			url = new URL(raw);
		} catch {
			return null;
		}
		if (url.protocol !== 'https:' || url.username !== '' || url.password !== '') return null;
		return url.href;
	}

	function openBrowser(): void {
		const href = httpsUrl(settings.githubVerificationUri);
		if (href === null) return;
		window.open(href, '_blank', 'noopener');
	}

	async function copyCode(): Promise<void> {
		try {
			await navigator.clipboard.writeText(settings.githubUserCode);
		} catch {
			// The code stays on screen when the clipboard is denied.
		}
	}

	const signedIn = $derived(
		settings.githubLogin ? `Signed in as ${settings.githubLogin}` : 'Connected',
	);
</script>

{#if !settings.githubEnabled}
	<p class="hint">{OFF}</p>
{:else if settings.githubPhase === 'pending'}
	<p class="code">{settings.githubUserCode}</p>
	<div class="actions">
		<button class="btn" type="button" onclick={copyCode}>Copy code</button>
		<button class="btn" type="button" onclick={openBrowser}>Open browser</button>
		<button class="btn btn--quiet" type="button" onclick={disconnectGithub}>Disconnect</button>
	</div>
{:else if settings.githubPhase === 'connected'}
	<p class="ok">{signedIn}</p>
	<div class="actions">
		<button class="btn btn--quiet" type="button" onclick={disconnectGithub}>Disconnect</button>
	</div>
{:else}
	{#if settings.githubMessage}
		<p class="hint">{settings.githubMessage}</p>
	{/if}
	<div class="actions">
		<button class="btn" type="button" onclick={connectGithub}>Connect GitHub</button>
		{#if settings.githubConnected}
			<button class="btn btn--quiet" type="button" onclick={disconnectGithub}>Disconnect</button>
		{/if}
	</div>
{/if}

<style>
	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: 0 0 var(--space-3);
	}

	.code {
		font-family: var(--font-mono);
		font-size: var(--text-sm);
		color: var(--color-ink);
		margin: 0 0 var(--space-3);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		margin-top: var(--space-3);
	}

	.btn {
		font-size: var(--text-sm);
		color: var(--color-on-accent);
		background: var(--color-accent);
		border: 0;
		border-radius: var(--radius-sm);
		padding: var(--space-2) var(--space-3);
		cursor: pointer;
	}

	.btn--quiet {
		color: var(--color-ink);
		background: var(--color-ground);
		border: 1px solid var(--color-hairline);
	}

	.ok {
		font-size: var(--text-sm);
		color: var(--color-ok);
		margin: 0;
	}
</style>
