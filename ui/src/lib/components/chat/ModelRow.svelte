<script lang="ts">
	// One model on the picker (TD-5236, split out of ModelPicker). The label
	// with its engine mark and quick-switch slot, the description (or what
	// the click will do instead), the check mark, and "Only this chat".
	import { modelDescription, modelLabel } from "../../effort";
	import { engineMark } from "../../model-engine";
	import {
		held,
		NEW_CHAT,
		offersOnlyHere,
		opensNewChat,
		pickHint,
		pickModel,
		pickOnlyHere,
	} from "../../model-pick";
	import { problemWith } from "../../model-ready.svelte.js";
	import { session } from "../../session-status.svelte.js";
	import { settings } from "../../settings.svelte.js";

	let { name, onpicked }: { name: string; onpicked: () => void } = $props();

	const label = $derived(modelLabel(name, settings.presetLabels));
	const mark = $derived(engineMark(name, settings.presetEngines));
	const slot = $derived(settings.presetSlots[name] ?? null);
	const description = $derived(modelDescription(name, settings.presetDescriptions));
	// TD-5240: what would stop this model running, as the daemon last said.
	const problem = $derived(problemWith(name));
</script>

<div class="row">
	<button
		class="item"
		type="button"
		role="menuitemradio"
		aria-checked={name === session.preset}
		disabled={held(name)}
		title={pickHint(name)}
		onclick={() => {
			pickModel(name);
			onpicked();
		}}
	>
		<span class="text">
			<span class="label"
				>{label}{#if mark}
					<span class="badge">{mark}</span>{/if}</span
			>
			{#if problem}
				<span class="sub sub--problem">{problem}</span>
			{:else if opensNewChat(name)}
				<span class="sub">{NEW_CHAT}</span>
			{:else if description}
				<span class="sub">{description}</span>
			{/if}
		</span>
		{#if slot !== null}
			<kbd class="slot" title={`Quick switch: ⌘⌥${slot} (Ctrl+Alt+${slot})`}>{slot}</kbd>
		{/if}
		<span class="check" aria-hidden="true">{name === session.preset ? "✓" : ""}</span>
	</button>
	{#if offersOnlyHere(name)}
		<button
			class="only"
			type="button"
			aria-label={`Only this chat: ${label}`}
			title="Use it for this chat and leave the model new chats use as it is"
			onclick={() => {
				pickOnlyHere(name);
				onpicked();
			}}>Only this chat</button
		>
	{/if}
</div>

<style>
	/* TD-5242: on a phone's narrow menu "Only this chat" drops under the
	   model's name instead of squeezing it or leaving the menu. */
	.row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: flex-end;
	}

	.item {
		flex: 1 1 9rem;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-1) var(--space-2);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		text-align: left;
		color: var(--color-ink);
		background: transparent;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
	}

	.item:hover:not(:disabled) {
		background: var(--color-sunken);
	}

	.item:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: -1px;
	}

	.item:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.text {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-width: 0;
	}

	.sub {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.sub--problem {
		color: var(--color-warn);
		white-space: normal;
	}

	.badge {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		background: var(--color-sunken);
		border-radius: var(--radius-full);
		padding: 0 var(--space-2);
		white-space: nowrap;
	}

	.slot {
		flex: none;
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: 0 var(--space-1);
	}

	.check {
		flex: none;
		width: 1em;
		text-align: center;
		color: var(--color-ink);
	}

	.only {
		flex: none;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		background: transparent;
		border: none;
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		white-space: nowrap;
	}

	.only:hover,
	.only:focus-visible {
		color: var(--color-ink);
		background: var(--color-sunken);
	}
</style>
