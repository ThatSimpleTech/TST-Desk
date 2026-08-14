<script lang="ts">
	// Transient toasts (TD-1008): self-expiring notices for failures that don't
	// block — rate limits, provider 5xx, context overflow. Each auto-dismisses
	// on the store's timer; the close button is the manual path. Lives in a
	// fixed stack so it never displaces the workspace layout.
	import { toasts, dismiss } from './notifications.svelte.js';
</script>

{#if toasts.length > 0}
	<div class="toast-stack" role="status" aria-live="polite">
		{#each toasts as toast (toast.id)}
			<div class="toast">
				<span class="dot" aria-hidden="true"></span>
				<div class="toast-text">
					<span class="toast-title">{toast.title}</span>
					<span class="toast-body">{toast.body}</span>
				</div>
				<button
					class="toast-close"
					type="button"
					aria-label="Dismiss {toast.title}"
					onclick={() => dismiss(toast.id)}>&times;</button
				>
			</div>
		{/each}
	</div>
{/if}

<style>
	.toast-stack {
		position: fixed;
		bottom: var(--space-4);
		right: var(--space-4);
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		width: min(22rem, calc(100vw - var(--space-8)));
		z-index: 100;
	}

	.toast {
		display: flex;
		align-items: flex-start;
		gap: var(--space-2);
		padding: var(--space-3);
		background: var(--color-bg-raised);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-lg);
	}

	.dot {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		background: var(--color-warning);
		margin-top: var(--space-1);
		flex-shrink: 0;
	}

	.toast-text {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		min-width: 0;
	}

	.toast-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-text);
	}

	.toast-body {
		font-size: var(--text-xs);
		color: var(--color-text-secondary);
		line-height: 1.4;
		/* Backticks in copy (e.g. `tstd keychain set`) wrap, never overflow. */
		overflow-wrap: break-word;
	}

	.toast-close {
		background: transparent;
		border: 0;
		padding: 0 var(--space-1);
		font-size: var(--text-lg);
		line-height: 1;
		color: var(--color-text-muted);
		cursor: pointer;
		flex-shrink: 0;
	}

	.toast-close:hover {
		color: var(--color-text);
	}
</style>
