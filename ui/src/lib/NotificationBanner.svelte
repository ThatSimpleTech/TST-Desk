<script lang="ts">
	// Blocking notifications (TD-1008): persistent banners for failures the
	// user must act on before work continues — missing/rejected API key, cap
	// pauses, session failures. Sits under the shell header so it's visible
	// from anywhere, and carries the "copy diagnostics" action (redacted
	// report — states, versions, recent event types only, never content, args,
	// or paths) for the report-it path in error copy.
	import { banners, dismiss, copyDiagnostics, notify } from './notifications.svelte.js';
	import { ws, daemon } from './connection-status.svelte.js';

	let copiedId = $state<number | null>(null);

	async function copyFor(id: number): Promise<void> {
		const ok = await copyDiagnostics({
			ws: ws.state,
			daemonState: daemon.state,
			daemonRestart: daemon.restart,
		});
		if (ok) {
			copiedId = id;
			setTimeout(() => {
				if (copiedId === id) copiedId = null;
			}, 2000);
		} else {
			notify('clipboard', {
				severity: 'toast',
				title: 'Copy failed',
				body: 'The clipboard is unavailable. The diagnostics are visible in the daemon log.',
			});
		}
	}
</script>

{#if banners.length > 0}
	<div class="banner-strip" role="alert">
		{#each banners as banner (banner.id)}
			<div class="nb">
				<span class="dot" aria-hidden="true"></span>
				<div class="nb-text">
					<span class="nb-title">{banner.title}</span>
					<span class="nb-body">{banner.body}</span>
				</div>
				<div class="nb-actions">
					<button class="nb-action" type="button" onclick={() => copyFor(banner.id)}>
						{copiedId === banner.id ? 'Copied' : 'Copy diagnostics'}
					</button>
					<button
						class="nb-close"
						type="button"
						aria-label="Dismiss {banner.title}"
						onclick={() => dismiss(banner.id)}>&times;</button
					>
				</div>
			</div>
		{/each}
	</div>
{/if}

<style>
	.banner-strip {
		display: flex;
		flex-direction: column;
		flex-shrink: 0;
	}

	.nb {
		display: flex;
		align-items: flex-start;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-6);
		background: var(--color-sunken);
		border-bottom: var(--border-width) solid var(--color-hairline);
		/* The stripe was written against a token nothing declares and lived on
		   its fallback; the vocabulary test in tokens.test.ts now says so. */
		border-left: 3px solid var(--color-err);
	}

	.dot {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		background: var(--color-err);
		margin-top: var(--space-1);
		flex-shrink: 0;
	}

	.nb-text {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		min-width: 0;
		flex: 1;
	}

	.nb-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}

	.nb-body {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		line-height: 1.4;
		overflow-wrap: break-word;
	}

	.nb-actions {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		flex-shrink: 0;
	}

	.nb-action {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-accent);
		background: transparent;
		border: var(--border-width) solid var(--color-accent);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.nb-action:hover {
		background: var(--color-accent);
		color: var(--color-on-accent);
	}

	.nb-close {
		background: transparent;
		border: 0;
		font-size: var(--text-lg);
		line-height: 1;
		color: var(--color-ink-muted);
		cursor: pointer;
	}

	.nb-close:hover {
		color: var(--color-ink);
	}
</style>
