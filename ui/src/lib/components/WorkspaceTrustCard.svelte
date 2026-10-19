<script lang="ts">
	// Workspace config trust card (TD-5113). A `.tst/config.yaml` that came
	// with the workspace and loosens the packaged defaults waits here, with
	// every loosened setting shown, until the user trusts or declines it.
	// Until then the daemon runs the session on the defaults.
	import {
		decideWorkspaceTrust,
		followingRemoteSession,
		session,
	} from '../session-status.svelte.js';
	import { trustCardMode } from '../workspace-trust';

	const mode = $derived(trustCardMode(session.configTrust));
	const remote = $derived(followingRemoteSession());
	// The declined notice opens into the same list, so the user can change
	// their mind. Closed again whenever the daemon re-announces.
	let reviewing = $state(false);
	$effect(() => {
		void session.configTrust;
		reviewing = false;
	});
</script>

{#if mode !== 'hidden' && session.configTrust !== null}
	<footer class="trust-bar" aria-label="Workspace config trust">
		{#if mode === 'ask' || reviewing}
			<article class="card" aria-label="Trust this workspace's config?">
				<header class="card-head">
					<span class="card-title">Trust this workspace's config?</span>
					<span class="badge">
						{session.configTrust.loosened.length} setting{session.configTrust.loosened
							.length === 1
							? ''
							: 's'}
					</span>
				</header>
				<p class="lede">
					<code>.tst/config.yaml</code> loosens TST Desk's defaults. Until you trust it, this
					workspace runs on the packaged defaults. Trust covers this exact file; a change asks again.
				</p>
				<ul class="rules">
					{#each session.configTrust.loosened as item, i (`${item.key}-${i}`)}
						<li class="rule">
							<span class="rule-key">{item.key}</span>
							<span class="rule-value">{item.value}</span>
							<span class="rule-default">default: {item.default}</span>
						</li>
					{/each}
				</ul>
				{#if remote}
					<p class="lede">Decide on the computer this session runs on.</p>
				{:else}
					<footer class="card-actions">
						<button
							class="btn btn--accept"
							type="button"
							onclick={() => decideWorkspaceTrust(true)}>Trust and apply</button
						>
						<button
							class="btn btn--reject"
							type="button"
							onclick={() => decideWorkspaceTrust(false)}>Keep defaults</button
						>
					</footer>
				{/if}
			</article>
		{:else}
			<div class="notice">
				<span>This workspace's config is not applied. You kept the defaults.</span>
				<button class="btn btn--link" type="button" onclick={() => (reviewing = true)}
					>Review</button
				>
			</div>
		{/if}
	</footer>
{/if}

<style>
	.trust-bar {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-top: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		max-height: 40%;
		overflow-y: auto;
		flex-shrink: 0;
	}

	.card {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-4);
		background: var(--color-ground);
		border: var(--border-width) solid var(--color-hairline);
		border-left: var(--space-1) solid var(--color-warn);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-sm);
	}

	.card-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
	}

	.card-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}

	.badge {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		padding: var(--space-1) var(--space-2);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		color: var(--color-ink-secondary);
		flex-shrink: 0;
	}

	.lede {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		line-height: var(--leading-normal);
	}

	.rules {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.rule {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 2fr) minmax(0, 1fr);
		gap: var(--space-2);
		padding: var(--space-2) var(--space-3);
		background: var(--color-sunken);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		font-size: var(--text-xs);
	}

	.rule-key,
	.rule-value {
		font-family: var(--font-mono);
		color: var(--color-ink);
		word-break: break-all;
	}

	.rule-key {
		font-weight: var(--weight-semibold);
	}

	.rule-default {
		color: var(--color-ink-muted);
	}

	.notice {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.card-actions {
		display: flex;
		justify-content: flex-end;
		gap: var(--space-2);
	}

	.btn {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-4);
		border: var(--border-width) solid transparent;
		border-radius: var(--radius-md);
		cursor: pointer;
	}

	.btn--accept {
		background: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn--accept:hover {
		background: var(--color-accent-hover);
	}

	.btn--reject {
		background: transparent;
		color: var(--color-ink);
		border-color: var(--color-hairline);
	}

	.btn--reject:hover {
		background: var(--color-sunken);
	}

	.btn--link {
		background: transparent;
		color: var(--color-accent);
		padding: var(--space-1) var(--space-2);
	}
</style>
