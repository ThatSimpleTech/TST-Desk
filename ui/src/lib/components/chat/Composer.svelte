<script lang="ts">
	// Message composer (TD-1004, restyled TD-1604): a lifted card — ≈24px
	// radius, hairline border, whisper shadow — holding the textarea and one
	// circular accent button that morphs send → stop while a turn runs
	// (Esc cancels too, TD-1609). Enter submits, Shift+Enter newlines,
	// auto-grows to eight rows before scrolling internally.
	import { shouldSubmit } from "../../chat-store";
	import Icon from "../Icon.svelte";

	let {
		disabled = false,
		running = false,
		onsubmit,
		oncancel,
	}: {
		disabled?: boolean;
		/** A turn is in flight; the send button becomes stop. */
		running?: boolean;
		onsubmit: (text: string) => void;
		oncancel?: () => void;
	} = $props();

	const MAX_ROWS = 8;

	let value = $state("");
	let textarea: HTMLTextAreaElement | null = $state(null);

	// Re-measure on every edit; cap growth at MAX_ROWS lines.
	$effect(() => {
		void value;
		if (textarea === null) return;
		const computed = parseFloat(getComputedStyle(textarea).lineHeight);
		const lineHeight = Number.isFinite(computed) ? computed : 24;
		const maxHeight = lineHeight * MAX_ROWS + 16;
		textarea.style.height = "auto";
		textarea.style.height = `${Math.min(textarea.scrollHeight, maxHeight)}px`;
	});

	function submit(): void {
		const text = value.trim();
		if (text === "" || disabled || running) return;
		onsubmit(text);
		value = "";
	}

	function handleKeydown(event: KeyboardEvent): void {
		if (shouldSubmit(event.key, event.shiftKey)) {
			event.preventDefault();
			submit();
		}
	}
</script>

<div class="composer">
	<div class="card">
		<textarea
			bind:this={textarea}
			bind:value
			rows="1"
			{disabled}
			placeholder={disabled ? "Waiting for a session…" : "Message the agent…"}
			aria-label="Message composer"
			onkeydown={handleKeydown}
		></textarea>
		{#if running && !disabled}
			<button
				type="button"
				class="send"
				title="Stop generating (Esc)"
				onclick={() => oncancel?.()}
				aria-label="Stop generating"
			>
				<Icon name="stop" size={14} filled />
			</button>
		{:else}
			<button
				type="button"
				class="send"
				{disabled}
				title="Send message"
				onclick={submit}
				aria-label="Send message"
			>
				<Icon name="arrow-up" size={18} />
			</button>
		{/if}
	</div>
	<p class="disclaimer">TST Desk can make mistakes — check its work.</p>
</div>

<style>
	.composer {
		padding: var(--space-2) var(--space-4) var(--space-3);
	}

	/* The card carries the chrome; the textarea inside is chromeless. */
	.card {
		display: flex;
		align-items: flex-end;
		gap: var(--space-2);
		padding: var(--space-2) var(--space-2) var(--space-2) var(--space-4);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-xl);
		box-shadow: var(--shadow-sm);
		transition: border-color var(--transition-fast);
	}

	.card:focus-within {
		border-color: var(--color-accent);
	}

	textarea {
		flex: 1;
		resize: none;
		padding: var(--space-2) 0;
		font-family: var(--font-sans);
		font-size: var(--text-base);
		line-height: var(--leading-normal);
		color: var(--color-ink);
		background: transparent;
		border: none;
		outline: none;
	}

	textarea:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.send {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		width: var(--space-8);
		height: var(--space-8);
		color: var(--color-on-accent);
		background: var(--color-accent);
		border: none;
		border-radius: var(--radius-full);
		cursor: pointer;
		transition: background var(--transition-fast);
	}

	.send:hover:not(:disabled) {
		background: var(--color-accent-hover);
	}

	.send:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.disclaimer {
		margin: 0;
		padding-top: var(--space-2);
		font-size: var(--text-xs);
		text-align: center;
		color: var(--color-ink-muted);
	}
</style>
