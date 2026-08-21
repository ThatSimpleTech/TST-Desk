<script lang="ts">
	// Approval card (TD-1007): the pending tool call, its classification and
	// reason, and the Approve/Deny actions. Presentational — decisions come
	// from approval.ts and the store; this just renders and forwards clicks.
	// Focus moves here on appearance (AC #3) so keyboard users land on the
	// decision they must make.
	import { onMount } from 'svelte';
	import { alwaysAllowLabel, isAlwaysAllowable, isDangerous } from '../approval';
	import { alwaysAllow, approve, deny } from '../approval-store.svelte.js';
	import type { PendingApproval } from '../approval';

	interface Props {
		approval: PendingApproval;
	}

	let { approval }: Props = $props();

	let dangerous = $derived(isDangerous(approval));
	let alwaysAllowable = $derived(isAlwaysAllowable(approval));
	let ruleLabel = $derived(alwaysAllowLabel(approval));
	let note = $state('');
	let cardEl: HTMLElement | null = null;

	onMount(() => cardEl?.focus());

	function pretty(value: unknown): string {
		if (typeof value === 'string') return value;
		return JSON.stringify(value, null, 2);
	}
</script>

<article
	class="card"
	class:card--danger={dangerous}
	tabindex="-1"
	bind:this={cardEl}
	data-approval-card={approval.toolCallId}
	aria-label={`Approval required: ${approval.summary}`}
>
	<header class="card-head">
		<span class="card-title">{approval.summary}</span>
		<span class="badge badge--{approval.decisionClass}">Class {approval.decisionClass}</span>
	</header>

	<dl class="card-fields">
		<div class="field">
			<dt class="field-key">Tool</dt>
			<dd class="field-value mono">{approval.toolName}</dd>
		</div>
		<div class="field">
			<dt class="field-key">Arguments</dt>
			<dd class="field-value"><pre class="code">{pretty(approval.arguments)}</pre></dd>
		</div>
		<div class="field">
			<dt class="field-key">Reason</dt>
			<dd class="field-value">{approval.reason}</dd>
		</div>
	</dl>

	<footer class="card-actions">
		<input
			class="note"
			type="text"
			placeholder="Note for the model (optional)"
			bind:value={note}
			aria-label="Optional denial note"
		/>
		<div class="buttons">
			<button class="btn btn--approve" type="button" onclick={() => approve(approval)}>Approve</button>
			{#if alwaysAllowable}
				<button
					class="btn btn--always"
					type="button"
					onclick={() => alwaysAllow(approval)}
					title={ruleLabel}
					aria-label={`Always allow in this workspace: ${ruleLabel}`}
				>Always allow in this workspace</button>
			{/if}
			<button class="btn btn--deny" type="button" onclick={() => deny(approval, note)}>Deny</button>
		</div>
	</footer>
</article>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-4);
		background: var(--color-bg);
		border: var(--border-width) solid var(--color-border);
		/* Class B (the default card) reads as a warning; class C turns the
		   leading edge danger-red so dangerous calls are distinct (AC #4). */
		border-left: var(--space-1) solid var(--color-warning);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-sm);
	}

	.card:focus {
		outline: 2px solid var(--color-accent);
		outline-offset: 2px;
	}

	.card--danger {
		border-left-color: var(--color-danger);
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
		color: var(--color-text);
	}

	.badge {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		padding: var(--space-1) var(--space-2);
		border: var(--border-width) solid transparent;
		border-radius: var(--radius-full);
		flex-shrink: 0;
	}

	.badge--A { color: var(--color-info); border-color: var(--color-info); }
	.badge--B { color: var(--color-warning); border-color: var(--color-warning); }
	.badge--C { color: var(--color-danger); border-color: var(--color-danger); }

	.card-fields {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.field-key {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-text-secondary);
	}

	.field-value {
		font-size: var(--text-sm);
		color: var(--color-text);
	}

	.mono {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
	}

	.code {
		margin: 0;
		padding: var(--space-2) var(--space-3);
		background: var(--color-bg-subtle);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		white-space: pre-wrap;
		word-break: break-word;
		max-height: var(--space-16);
		overflow-y: auto;
	}

	.card-actions {
		display: flex;
		align-items: center;
		gap: var(--space-3);
	}

	.note {
		flex: 1;
		min-width: 0;
		font-size: var(--text-sm);
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-md);
		background: var(--color-bg);
		color: var(--color-text);
	}

	.note:focus {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
	}

	.buttons {
		display: flex;
		gap: var(--space-2);
		flex-shrink: 0;
	}

	.btn {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-4);
		border: var(--border-width) solid transparent;
		border-radius: var(--radius-md);
		cursor: pointer;
	}

	.btn--approve {
		background: var(--color-accent);
		color: var(--color-accent-text);
	}

	.btn--approve:hover {
		background: var(--color-accent-hover);
	}

	.btn--deny {
		background: transparent;
		color: var(--color-danger);
		border-color: var(--color-danger);
	}

	.btn--deny:hover {
		background: var(--color-danger);
		color: var(--color-accent-text);
	}

	.btn--always {
		background: transparent;
		color: var(--color-text);
		border-color: var(--color-border);
	}

	.btn--always:hover {
		background: var(--color-bg-subtle);
		border-color: var(--color-text-secondary);
	}
</style>
