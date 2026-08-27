<script lang="ts">
	// Hold-to-talk control (TD-4701). Hidden unless setup_state says
	// speech_enabled. The mic opens only while this is held.
	import {
		beginHold,
		cancelHold,
		dictation,
		endHold,
		onTranscript,
		startDictation,
	} from "../../dictation.svelte.js";
	import { settings } from "../../settings.svelte.js";
	import Icon from "../Icon.svelte";

	let {
		disabled = false,
		ontranscript,
	}: {
		disabled?: boolean;
		ontranscript: (text: string) => void;
	} = $props();

	$effect(() => {
		const stop = startDictation();
		const off = onTranscript(ontranscript);
		return () => {
			off();
			stop();
		};
	});

	function label(): string {
		if (dictation.transcribing) return "Transcribing…";
		if (dictation.holding) return "Release to transcribe";
		if (dictation.error) return dictation.error;
		if (!settings.speechReady) {
			return "Set speech.base_url in config.yaml to a transcription endpoint. There is no cloud default.";
		}
		return "Hold to talk";
	}

	async function down(event: PointerEvent): Promise<void> {
		if (event.button !== 0 || disabled || dictation.transcribing) return;
		event.preventDefault();
		(event.currentTarget as HTMLButtonElement).setPointerCapture(event.pointerId);
		await beginHold();
	}
</script>

{#if settings.speechEnabled}
	<button
		type="button"
		class="mic"
		class:holding={dictation.holding}
		class:busy={dictation.transcribing}
		disabled={disabled || dictation.transcribing}
		title={label()}
		aria-label={label()}
		aria-pressed={dictation.holding}
		onpointerdown={down}
		onpointerup={() => void endHold()}
		onpointercancel={cancelHold}
	>
		<Icon name="mic" size={16} />
	</button>
	{#if dictation.error}
		<span class="live" role="status">{dictation.error}</span>
	{/if}
{/if}

<style>
	.mic {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		width: var(--space-8);
		height: var(--space-8);
		color: var(--color-ink-muted);
		background: transparent;
		border: none;
		border-radius: var(--radius-full);
		cursor: pointer;
		transition:
			color var(--transition-fast),
			background var(--transition-fast);
	}

	.mic:hover:not(:disabled) {
		color: var(--color-ink);
		background: var(--color-sunken);
	}

	.mic:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
	}

	.mic:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.mic.holding {
		color: var(--color-on-accent);
		background: var(--color-accent);
		opacity: 1;
		cursor: pointer;
	}

	.mic.busy {
		cursor: wait;
	}

	.live {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
</style>
