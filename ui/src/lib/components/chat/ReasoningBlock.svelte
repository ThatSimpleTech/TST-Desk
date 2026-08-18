<script lang="ts">
	// A reasoning model's thinking, folded (TD-1902). Sits above the answer
	// in the same message row.
	//
	// The disclosure is the resting state, not a preference to find: at the
	// local brain tier's measured throughput a 27B thinker out-produces its
	// own answer several times over, so an unfolded transcript buries the
	// reply it exists to deliver. Open while thinking is the live thing,
	// closed the moment the answer starts — unless the reader said otherwise,
	// which wins permanently (see ../../reasoning-disclosure.svelte.ts).
	//
	// Presentational only: every rule lives in that module so it can be
	// tested without a DOM, and the toggle map lives there because the
	// message list windows its rows and would otherwise reset this on scroll.
	import type { ChatMessage } from "../../chat-store";
	import {
		isExpanded,
		isThinkingLive,
		thoughtLabel,
		toggle,
	} from "../../reasoning-disclosure.svelte.js";
	import Icon from "../Icon.svelte";

	let { message }: { message: ChatMessage } = $props();

	// Read the rule straight out of the reactive module — no local mirror,
	// so nothing can drift and a recycled row cannot inherit another
	// message's fold.
	const live = $derived(isThinkingLive(message));
	const expanded = $derived(isExpanded(message));
	const label = $derived(thoughtLabel(message));
</script>

<div class="reasoning" class:live>
	<button
		class="summary"
		type="button"
		aria-expanded={expanded}
		onclick={() => toggle(message)}
	>
		<span class="chevron" class:open={expanded} aria-hidden="true">
			<Icon name="chevron-down" size={11} />
		</span>
		<span class="label" class:shimmer={live}>{label}</span>
	</button>
	{#if expanded}
		<!-- Selectable and copyable: it is text the reader may want to quote
		     back, and a disclosure that fights selection is a worse one. -->
		<div class="body">{message.reasoning}</div>
	{/if}
</div>

<style>
	.reasoning {
		margin-bottom: var(--space-3);
	}

	.summary {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-1) 0;
		border: none;
		background: transparent;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		cursor: pointer;
	}

	.summary:hover .label {
		color: var(--color-ink);
	}

	.chevron {
		display: inline-flex;
		transform: rotate(-90deg);
		transition: transform var(--transition-fast);
	}

	.chevron.open {
		transform: rotate(0deg);
	}

	.body {
		margin-top: var(--space-2);
		padding-left: var(--space-3);
		border-left: var(--border-width) solid var(--color-hairline);
		font-size: var(--text-sm);
		line-height: var(--leading-relaxed);
		color: var(--color-ink-muted);
		white-space: pre-wrap;
		user-select: text;
	}

	/* Matches the composer's working shimmer (TD-1607) so live thinking
	   reads as the same state, not a second vocabulary for it. */
	.shimmer {
		background: linear-gradient(
			100deg,
			var(--color-ink-muted) 40%,
			var(--color-ink) 50%,
			var(--color-ink-muted) 60%
		);
		background-size: 200% 100%;
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
		animation: reasoning-shimmer 1.6s linear infinite;
	}

	@keyframes reasoning-shimmer {
		from {
			background-position: 100% 0;
		}
		to {
			background-position: -100% 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.chevron {
			transition: none;
		}

		.shimmer {
			animation: none;
			background: none;
			color: var(--color-ink-muted);
		}
	}
</style>
