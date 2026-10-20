<script lang="ts">
	// Question card (TD-5121): an `ask_user` call's questions, with each
	// option as a button and a box for the person's own words. One
	// single-choice question sends on a tap; otherwise Send sends them all.
	// Skip (or Esc) tells the model the person chose not to answer. The
	// phone shows this same card, so every option is a full-size tap target.
	import { onMount, untrack } from 'svelte';
	import Icon from './Icon.svelte';
	import { canSend, emptyDrafts, sendsOnTap, toggle, type Draft } from '../question';
	import { answer, canAnswer, decline, type QuestionCard } from '../question-store.svelte.js';
	import { approvalWaitCopy } from '../machines';
	import { machineNameFor } from '../machines.svelte.js';

	interface Props {
		card: QuestionCard;
	}

	let { card }: Props = $props();

	// The bar keys each card by its tool call id, so one instance is one
	// question set: its picks start empty once and live here.
	let drafts = $state<Draft[]>(untrack(() => emptyDrafts(card)));
	let cardEl: HTMLElement | null = null;

	let waiting = $derived(canAnswer(card) ? null : approvalWaitCopy(machineNameFor(card.sessionId) ?? ''));
	let locked = $derived(card.sent || waiting !== null);
	let ready = $derived(canSend(drafts));
	let title = $derived(
		card.questions.length === 1 ? 'The agent has a question' : `The agent has ${card.questions.length} questions`
	);

	onMount(() => cardEl?.focus());

	function pick(index: number, label: string): void {
		if (locked) return;
		const question = card.questions[index];
		const draft = drafts[index];
		if (question === undefined || draft === undefined) return;
		const next = toggle(question, draft, label);
		drafts[index] = next;
		if (sendsOnTap(card) && next.selected.length === 1 && next.text.trim() === '') {
			answer(card, drafts);
		}
	}

	function setText(index: number, text: string): void {
		const draft = drafts[index];
		if (draft !== undefined) drafts[index] = { ...draft, text };
	}

	function ownWords(index: number): string {
		return drafts[index]?.text ?? '';
	}

	function submit(): void {
		if (locked || !ready) return;
		answer(card, drafts);
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.defaultPrevented) return;
		if (event.key === 'Escape') {
			// Esc is Skip here, and stops short of the shell's global handler,
			// which would cancel the turn behind the question.
			event.preventDefault();
			event.stopPropagation();
			if (!locked) decline(card);
		}
	}

	function onTextKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			submit();
		}
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<article
	class="card"
	tabindex="-1"
	bind:this={cardEl}
	data-question-card={card.toolCallId}
	aria-label={title}
	onkeydown={onKeydown}
>
	<header class="head">
		<span class="head-icon" aria-hidden="true"><Icon name="message-square" size={16} /></span>
		<span class="title">{title}</span>
	</header>

	{#each card.questions as question, index (index)}
		<fieldset class="question" disabled={locked}>
			<legend class="ask">
				{#if question.header}<span class="chip">{question.header}</span>{/if}
				<span class="text">{question.question}</span>
				{#if question.multi_select}<span class="hint">Pick any that apply</span>{/if}
			</legend>
			<div class="options">
				{#each question.options as option (option.label)}
					{@const chosen = drafts[index]?.selected.includes(option.label) ?? false}
					<button
						type="button"
						class="option"
						class:option--chosen={chosen}
						aria-pressed={chosen}
						onclick={() => pick(index, option.label)}
					>
						<span class="label">{option.label}</span>
						{#if option.description}<span class="detail">{option.description}</span>{/if}
					</button>
				{/each}
			</div>
			<input
				class="own"
				class:own--used={ownWords(index).trim() !== ''}
				type="text"
				placeholder="Or type your own answer"
				aria-label={`Your own answer to: ${question.question}`}
				value={ownWords(index)}
				oninput={(event) => setText(index, event.currentTarget.value)}
				onkeydown={onTextKeydown}
			/>
		</fieldset>
	{/each}

	<footer class="actions">
		{#if card.notice !== null}<p class="notice" role="alert">{card.notice}</p>{/if}
		{#if waiting !== null}<p class="waiting" role="status">{waiting}</p>{/if}
		{#if card.sent}<p class="waiting" role="status">Sending your answer…</p>{/if}
		<div class="buttons">
			<button class="btn btn--skip" type="button" disabled={locked} onclick={() => decline(card)}>
				Skip <kbd aria-hidden="true">Esc</kbd>
			</button>
			<button class="btn btn--send" type="button" disabled={locked || !ready} onclick={submit}>
				Send answer
			</button>
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
		border-left: var(--space-1) solid var(--color-accent);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-md);
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.card:focus {
		outline: 2px solid var(--color-accent);
		outline-offset: 2px;
	}

	.head {
		display: flex;
		align-items: center;
		gap: var(--space-3);
	}

	.head-icon {
		display: inline-flex;
		color: var(--color-accent);
		flex-shrink: 0;
	}

	.title {
		font-size: var(--text-base);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
	}

	.question {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		margin: 0;
		padding: 0;
		border: none;
		min-width: 0;
	}

	.ask {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: var(--space-2);
		padding: 0;
		margin-bottom: var(--space-2);
	}

	.chip {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-accent);
		border: var(--border-width) solid var(--color-accent);
		border-radius: var(--radius-full);
		padding: 0 var(--space-2);
		line-height: var(--leading-relaxed);
	}

	.text {
		font-size: var(--text-sm);
		color: var(--color-ink);
		overflow-wrap: anywhere;
	}

	.hint {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.options {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
	}

	.option {
		display: inline-flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 2px;
		min-width: 8rem;
		max-width: 100%;
		padding: var(--space-2) var(--space-3);
		text-align: left;
		background: var(--color-ground);
		color: var(--color-ink);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		cursor: pointer;
		transition:
			background var(--transition-fast),
			border-color var(--transition-fast);
	}

	.option:hover:not(:disabled) {
		border-color: var(--color-accent);
	}

	.option--chosen {
		border-color: var(--color-accent);
		background: var(--color-accent);
		color: var(--color-on-accent);
	}

	.option:disabled {
		cursor: not-allowed;
		opacity: 0.6;
	}

	.label {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
	}

	.detail {
		font-size: var(--text-xs);
		opacity: 0.8;
	}

	.own {
		font-size: var(--text-sm);
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-ground);
		color: var(--color-ink);
	}

	.own--used {
		border-color: var(--color-accent);
	}

	.own::placeholder {
		color: var(--color-ink-muted);
	}

	.actions {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.notice {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-err);
	}

	.waiting {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.buttons {
		display: flex;
		gap: var(--space-2);
		justify-content: flex-end;
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
	}

	.btn:disabled {
		cursor: not-allowed;
		opacity: 0.6;
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

	.btn--send {
		background: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn--send:hover:not(:disabled) {
		background: var(--color-accent-hover);
	}

	.btn--skip {
		background: transparent;
		color: var(--color-ink-secondary);
		border-color: var(--color-hairline);
	}

	/* On a phone each option is a full-width tap target. */
	@media (pointer: coarse), (max-width: 639px) {
		.options {
			flex-direction: column;
		}

		.option,
		.btn {
			min-height: 44px;
		}

		.option {
			width: 100%;
		}

		.btn kbd {
			display: none;
		}
	}
</style>
