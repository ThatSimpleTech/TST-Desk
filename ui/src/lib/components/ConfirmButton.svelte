<script lang="ts">
	// A destructive row action that asks first (INTERACT-12, SETMODEL-05).
	// The first click turns the button into "<question> Remove · Cancel"; the
	// confirming button is inert for ARM_MS and counts only a press that began
	// after it armed, so a double-click's second press never confirms. Once
	// it fires, every row action ignores input for ROW_QUIET_MS.
	import { onDestroy } from 'svelte';
	import { ARM_MS, clickDecides, newArming, notePress } from '../approval-arming';
	import { rowActionFired, rowsQuiet } from '../row-guard';

	interface Props {
		label: string;
		/** The question, naming what goes: "Remove the key Alpha? …". */
		question: string;
		confirmLabel?: string;
		ariaLabel?: string;
		buttonClass?: string;
		disabled?: boolean;
		onconfirm: () => void;
	}

	let {
		label,
		question,
		confirmLabel = label,
		ariaLabel,
		buttonClass = 'btn btn--danger',
		disabled = false,
		onconfirm,
	}: Props = $props();

	let asking = $state(false);
	let arming = $state(newArming());
	let timer: ReturnType<typeof setTimeout> | null = null;

	function ask(): void {
		if (disabled || rowsQuiet()) return;
		asking = true;
		arming = newArming();
		timer = setTimeout(() => (arming = { ...arming, armed: true }), ARM_MS);
	}

	function cancel(): void {
		asking = false;
		if (timer !== null) clearTimeout(timer);
	}

	function press(): void {
		arming = notePress(arming);
	}

	function confirm(event: MouseEvent): void {
		if (!clickDecides(arming, event.detail)) return;
		asking = false;
		rowActionFired();
		onconfirm();
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.repeat) {
			event.preventDefault();
			return;
		}
		if (event.key === 'Escape') {
			event.stopPropagation();
			cancel();
		}
	}

	onDestroy(() => {
		if (timer !== null) clearTimeout(timer);
	});
</script>

{#if asking}
	<span class="ask" role="group" aria-label={question} onkeydowncapture={onKeydown}>
		<span class="question">{question}</span>
		<button
			class={buttonClass}
			class:arming={!arming.armed}
			type="button"
			aria-disabled={!arming.armed}
			onpointerdown={press}
			onclick={confirm}>{confirmLabel}</button
		>
		<button class="btn" type="button" onclick={cancel}>Cancel</button>
	</span>
{:else}
	<button class={buttonClass} type="button" aria-label={ariaLabel} {disabled} onclick={ask}
		>{label}</button
	>
{/if}

<style>
	.ask {
		display: inline-flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-2);
	}

	.question {
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	/* The buttons render here, so the variants a caller names are styled
	   here, matching the Settings rows they sit in. */
	.btn,
	.revoke,
	.link {
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.link {
		border: 0;
		color: var(--color-ink-secondary);
	}

	.btn:hover:not(:disabled),
	.revoke:hover {
		border-color: var(--color-accent);
	}

	.btn--danger:hover:not(:disabled) {
		border-color: var(--color-err);
		color: var(--color-err);
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.arming {
		opacity: 0.55;
		cursor: default;
	}
</style>
