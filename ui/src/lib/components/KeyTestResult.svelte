<script lang="ts">
	// One saved key's Test result, on its own row (TD-5200). The live region
	// is always in the DOM, so a screen reader hears the verdict when it lands.
	import type { IconName } from '../icons';
	import type { KeyTest, KeyTestResult } from '../key-tests.svelte.js';
	import Icon from './Icon.svelte';

	let { test }: { test: KeyTest | undefined } = $props();

	const LOOK: Record<KeyTestResult['state'], { label: string; icon: IconName }> = {
		ok: { label: 'Connected', icon: 'check' },
		failed: { label: 'Not connected', icon: 'x' },
		no_answer: { label: 'No answer', icon: 'alert' },
	};

	const result = $derived(test === undefined || test.state === 'testing' ? null : test);
</script>

<div class="key-test" role="status" aria-live="polite" aria-atomic="true">
	{#if result}
		<p class="verdict verdict--{result.state}">
			<span class="pill"><Icon name={LOOK[result.state].icon} />{LOOK[result.state].label}</span>
			{#if result.detail}<span class="detail">{result.detail}</span>{/if}
		</p>
	{/if}
</div>

<style>
	/* A flex item of the row's actions: beside the buttons while there is
	   room, on its own line below them when there is not. */
	.key-test {
		flex: 1 1 12rem;
		min-width: 0;
	}

	.verdict {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: var(--space-1) var(--space-2);
		margin: 0;
		font-size: var(--text-sm);
		line-height: var(--leading-normal);
		color: var(--color-ink-secondary);
	}

	.pill {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		flex-shrink: 0;
		padding: 0 var(--space-2);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		line-height: var(--leading-relaxed);
		border: var(--border-width) solid currentColor;
		border-radius: var(--radius-full);
	}

	.detail {
		min-width: 0;
		overflow-wrap: anywhere;
	}

	.verdict--ok .pill {
		color: var(--color-ok);
		background: color-mix(in srgb, var(--color-ok) 10%, transparent);
	}

	.verdict--failed .pill {
		color: var(--color-err);
		background: color-mix(in srgb, var(--color-err) 10%, transparent);
	}

	.verdict--failed .detail {
		color: var(--color-err);
	}

	.verdict--no_answer .pill {
		color: var(--color-warn);
		background: color-mix(in srgb, var(--color-warn) 10%, transparent);
	}
</style>
