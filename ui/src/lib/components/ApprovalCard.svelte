<script lang="ts">
	// Approval card (TD-1007): the pending tool call, its classification and
	// reason, and the Approve/Deny actions. Presentational — decisions come
	// from approval.ts and the store; this just renders and forwards clicks.
	// Focus moves here on appearance (AC #3) so keyboard users land on the
	// decision they must make: ⏎ approves, Esc denies, from the card itself.
	import { onMount } from 'svelte';
	import Icon from './Icon.svelte';
	import { alwaysAllowLabel, isAlwaysAllowable, isDangerous } from '../approval';
	import { alwaysAllow, approve, deny } from '../approval-store.svelte.js';
	import { classifyDiffLine, diffLines } from '../entry-view';
	import type { PendingApproval } from '../approval';

	interface Props {
		approval: PendingApproval;
	}

	let { approval }: Props = $props();

	let dangerous = $derived(isDangerous(approval));
	let alwaysAllowable = $derived(isAlwaysAllowable(approval));
	let ruleLabel = $derived(alwaysAllowLabel(approval));
	let note = $state('');
	// "Always allow" is a scope on the approval, not a third verb: ticking it
	// turns Approve into the rule-writing form of itself.
	let remember = $state(false);
	let cardEl: HTMLElement | null = null;

	onMount(() => cardEl?.focus());

	// The body leads with the one argument that says what will happen — the
	// diff for a write, the command for a shell call — and folds the rest
	// under it. A wall of JSON made every approval look like the same one.
	const LEAD_KEYS = ['command', 'cmd', 'content', 'path', 'url', 'query', 'pattern'] as const;

	let args = $derived(
		typeof approval.arguments === 'object' &&
			approval.arguments !== null &&
			!Array.isArray(approval.arguments)
			? (approval.arguments as Record<string, unknown>)
			: {}
	);
	let diff = $derived(typeof args.diff === 'string' ? args.diff : null);
	let lead = $derived.by(() => {
		for (const key of LEAD_KEYS) {
			const value = args[key];
			if (typeof value === 'string' && value.trim() !== '') return { key, value };
		}
		return null;
	});
	let rest = $derived.by(() => {
		const entries = Object.entries(args).filter(
			([key]) => key !== 'diff' && key !== lead?.key
		);
		return entries.length === 0 ? null : JSON.stringify(Object.fromEntries(entries), null, 2);
	});

	function decide(): void {
		if (remember && alwaysAllowable) alwaysAllow(approval);
		else approve(approval);
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.defaultPrevented) return;
		if (event.key === 'Enter' && event.target === cardEl) {
			event.preventDefault();
			decide();
		} else if (event.key === 'Escape') {
			// Esc inside the card is Deny — the layer to peel is the decision
			// itself, not the turn behind it. Stop it short of the shell's
			// global handler, which would cancel that turn.
			event.preventDefault();
			event.stopPropagation();
			deny(approval, note);
		}
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<article
	class="card"
	class:card--danger={dangerous}
	tabindex="-1"
	bind:this={cardEl}
	data-approval-card={approval.toolCallId}
	aria-label={`Approval required: ${approval.summary}`}
	onkeydown={onKeydown}
>
	<header class="head">
		<span class="head-icon" aria-hidden="true"><Icon name="alert" size={16} /></span>
		<div class="head-text">
			<span class="title">{approval.summary}</span>
			<span class="meta">
				<code class="tool">{approval.toolName}</code>
				<span class="badge badge--{approval.decisionClass}">
					Class {approval.decisionClass}{dangerous ? ' · dangerous' : ''}
				</span>
			</span>
		</div>
	</header>

	<div class="body">
		{#if diff !== null}
			<pre class="code diff" aria-label="Proposed change">{#each diffLines(diff) as line}<span class="diff-line diff-line--{classifyDiffLine(line)}">{line}</span>{'\n'}{/each}</pre>
		{/if}
		{#if lead !== null}
			<div class="lead">
				<span class="key">{lead.key}</span>
				<pre class="code">{lead.value}</pre>
			</div>
		{/if}
		{#if rest !== null}
			<details class="rest">
				<summary>{lead === null && diff === null ? 'Arguments' : 'All arguments'}</summary>
				<pre class="code">{rest}</pre>
			</details>
		{/if}
		<p class="reason">{approval.reason}</p>
	</div>

	<footer class="actions">
		{#if alwaysAllowable}
			<label class="remember" title={ruleLabel}>
				<input
					type="checkbox"
					bind:checked={remember}
					aria-label={`Always allow in this workspace: ${ruleLabel}`}
				/>
				<span>Always allow this in this workspace</span>
			</label>
		{/if}
		<div class="decide">
			<input
				class="note"
				type="text"
				placeholder="Note for the model (optional)"
				bind:value={note}
				aria-label="Optional denial note"
			/>
			<div class="buttons">
				<button class="btn btn--deny" type="button" onclick={() => deny(approval, note)}>
					Deny <kbd aria-hidden="true">Esc</kbd>
				</button>
				<button class="btn btn--approve" type="button" onclick={decide}>
					{remember && alwaysAllowable ? 'Always allow' : 'Approve'} <kbd aria-hidden="true">⏎</kbd>
				</button>
			</div>
		</div>
	</footer>
</article>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-4);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		/* Class B (the default card) reads as a warning; class C turns the
		   leading edge danger-red so dangerous calls are distinct (AC #4). */
		border-left: var(--space-1) solid var(--color-warn);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-md);
		animation: rise var(--dur-enter) var(--ease-out);
	}

	/* The card takes focus programmatically, so the ring is drawn for plain
	   :focus too — a keyboard user must see where the decision landed. */
	.card:focus {
		outline: 2px solid var(--color-accent);
		outline-offset: 2px;
	}

	.card--danger {
		border-left-color: var(--color-err);
	}

	.head {
		display: flex;
		align-items: flex-start;
		gap: var(--space-3);
	}

	.head-icon {
		display: inline-flex;
		margin-top: 2px;
		color: var(--color-warn);
		flex-shrink: 0;
	}

	.card--danger .head-icon {
		color: var(--color-err);
	}

	.head-text {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		min-width: 0;
	}

	.title {
		font-size: var(--text-base);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		overflow-wrap: anywhere;
	}

	.meta {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.tool {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.badge {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		padding: 0 var(--space-2);
		border: var(--border-width) solid transparent;
		border-radius: var(--radius-full);
		line-height: var(--leading-relaxed);
		flex-shrink: 0;
	}

	.badge--A { color: var(--color-accent); border-color: var(--color-accent); }
	.badge--B { color: var(--color-warn); border-color: var(--color-warn); }
	.badge--C { color: var(--color-err); border-color: var(--color-err); }

	.body {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.lead {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.key {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-ink-secondary);
	}

	.code {
		margin: 0;
		padding: var(--space-2) var(--space-3);
		background: var(--color-sunken);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		white-space: pre-wrap;
		word-break: break-word;
		max-height: var(--space-16);
		overflow-y: auto;
	}

	/* The diff gets more room than an argument dump: it is the decision. */
	.diff {
		white-space: pre;
		overflow-x: auto;
		max-height: calc(var(--space-16) * 2);
	}

	.diff-line { display: inline; }
	.diff-line--add { color: var(--color-ok); }
	.diff-line--del { color: var(--color-err); }
	.diff-line--hunk { color: var(--color-accent); }
	.diff-line--meta { color: var(--color-ink-muted); font-weight: var(--weight-semibold); }
	.diff-line--context { color: var(--color-ink-secondary); }

	.rest summary {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		cursor: pointer;
		margin-bottom: var(--space-1);
	}

	.rest summary:hover {
		color: var(--color-ink);
	}

	.reason {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.actions {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.remember {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		cursor: pointer;
		align-self: flex-start;
	}

	.remember input {
		accent-color: var(--color-accent);
		margin: 0;
	}

	.decide {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--space-3);
	}

	.note {
		flex: 1;
		min-width: 12rem;
		font-size: var(--text-sm);
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-ground);
		color: var(--color-ink);
	}

	.note::placeholder {
		color: var(--color-ink-muted);
	}

	.buttons {
		display: flex;
		gap: var(--space-2);
		flex-shrink: 0;
		margin-left: auto;
	}

	.btn {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-4);
		border: var(--border-width) solid transparent;
		border-radius: var(--radius-md);
		cursor: pointer;
		transition:
			background var(--transition-fast),
			border-color var(--transition-fast),
			color var(--transition-fast);
	}

	.btn kbd {
		font-family: var(--font-sans);
		font-size: 10px;
		line-height: 1;
		padding: 2px 4px;
		border: 1px solid currentColor;
		border-radius: var(--radius-sm);
		opacity: 0.6;
	}

	/* Approve is the one filled control on the card: the action you take
	   nine times in ten should be the one that looks like a button. */
	.btn--approve {
		background: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn--approve:hover {
		background: var(--color-accent-hover);
	}

	/* Deny stays quiet until pointed at; a red outline next to every
	   approval made the safe path look like the alarming one. */
	.btn--deny {
		background: transparent;
		color: var(--color-ink-secondary);
		border-color: var(--color-hairline);
	}

	.btn--deny:hover {
		color: var(--color-err);
		border-color: var(--color-err);
	}
</style>
