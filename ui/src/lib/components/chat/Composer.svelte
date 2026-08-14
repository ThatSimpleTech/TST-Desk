<script lang="ts">
	// Message composer (TD-1004): multi-line, Enter submits, Shift+Enter
	// newlines, auto-grows to eight rows before scrolling internally.
	import { shouldSubmit } from "../../chat-store";

	let {
		disabled = false,
		onsubmit,
	}: {
		disabled?: boolean;
		onsubmit: (text: string) => void;
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
		if (text === "" || disabled) return;
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
	<textarea
		bind:this={textarea}
		bind:value
		rows="1"
		{disabled}
		placeholder={disabled ? "Waiting for a session…" : "Message the agent…"}
		aria-label="Message composer"
		onkeydown={handleKeydown}
	></textarea>
	<button type="button" class="send" {disabled} onclick={submit} aria-label="Send message">↑</button>
</div>

<style>
	.composer {
		display: flex;
		align-items: flex-end;
		gap: var(--space-2);
		padding: var(--space-3) var(--space-4);
		border-top: var(--border-width) solid var(--color-border);
		background: var(--color-bg-raised);
	}

	textarea {
		flex: 1;
		resize: none;
		padding: var(--space-2) var(--space-3);
		font-family: var(--font-family);
		font-size: var(--text-base);
		line-height: var(--leading-normal);
		color: var(--color-text);
		background: var(--color-bg);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-md);
		outline: none;
		transition: border-color var(--transition-fast);
	}

	textarea:focus {
		border-color: var(--color-accent);
	}

	textarea:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.send {
		flex-shrink: 0;
		width: var(--space-8);
		height: var(--space-8);
		font-size: var(--text-lg);
		color: var(--color-accent-text);
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
</style>
