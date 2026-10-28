<!--
  "Find models on this computer" (TD-5180), inside Add a model's URL step.
  The daemon asks only the loopback servers config lists, and only when the
  user clicks. A server that answered goes straight to the model pick: it is
  on this computer, so there is no key step.
-->
<script lang="ts">
	import { chooseLocalServer, onboarding, scanLocalServers } from '../onboarding.svelte.js';

	const form = $derived(onboarding.model);

	function port(url: string): string {
		try {
			return new URL(url).port || url;
		} catch {
			return url;
		}
	}

	const looked = $derived(form.scan ? form.scan.checked.map(port).join(', ') : '');
</script>

<div class="scan">
	<button class="btn" type="button" disabled={form.scanning} onclick={scanLocalServers}>
		{form.scan ? 'Look again' : 'Find models on this computer'}
	</button>
	{#if form.scanning}
		<p class="verdict" role="status">Looking for model servers on this computer…</p>
	{:else if form.scan && form.scan.servers.length > 0}
		<div class="preset-list" role="group" aria-label="Model servers on this computer">
			{#each form.scan.servers as server (server.base_url)}
				<button class="preset" type="button" onclick={() => chooseLocalServer(server)}>
					<span class="preset-name">{server.name}</span>
					<span class="preset-meta">
						{server.models.length} model{server.models.length === 1 ? '' : 's'} ·
						<span class="mono">{server.base_url}</span>
					</span>
				</button>
			{/each}
		</div>
	{:else if form.scan && form.scan.checked.length === 0}
		<p class="verdict" role="status">
			No servers to look for: <span class="mono">local_servers</span> in config.yaml is empty.
		</p>
	{:else if form.scan}
		<p class="verdict" role="status">
			Nothing answered on this computer (ports {looked}). Start your model server, then look
			again.
		</p>
	{/if}
</div>

<style>
	.scan {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--space-2);
	}

	.btn {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-3);
		border-radius: var(--radius-md);
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-ground);
		color: var(--color-ink);
		cursor: pointer;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.verdict {
		margin: 0;
		font-size: var(--text-xs);
		line-height: 1.4;
		color: var(--color-ink-secondary);
	}

	.preset-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		align-self: stretch;
	}

	.preset {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		padding: var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-ground);
		cursor: pointer;
		text-align: left;
	}

	.preset-name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
	}

	.preset-meta {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		white-space: nowrap;
	}

	.mono {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
	}
</style>
