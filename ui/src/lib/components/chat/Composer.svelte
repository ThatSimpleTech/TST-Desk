<script lang="ts">
	// Message composer (TD-1004, restyled TD-1604): a lifted card — ≈24px
	// radius, hairline border, whisper shadow — holding the textarea and one
	// circular accent button that morphs send → stop while a turn runs
	// (Esc cancels too, TD-1609). Enter submits, Shift+Enter newlines,
	// auto-grows to eight rows before scrolling internally.
	//
	// Text files attach here three ways (TD-1709): the paperclip's picker,
	// drag-and-drop onto the card, and paste. The refusal shown inline is a
	// courtesy — the daemon refuses the same file again on arrival, and that
	// is the gate that actually holds. Deliberately no `accept` filter on the
	// picker: a file the user cannot even select produces no copy explaining
	// why, and the copy is the point.
	import {
		acceptAttachment,
		toChips,
		type AttachmentDraft,
		type AttachmentRefusal,
	} from "../../attachments";
	import { shouldSubmit } from "../../chat-store";
	import { commands, loadCommands } from "../../commands.svelte.js";
	import { acceptPickDrafts } from "../../design";
	import { clearPicks, design, removePick } from "../../design.svelte.js";
	import type { AttachmentLimits, CommandEntry } from "../../protocol";
	import { session } from "../../session-status.svelte.js";
	import { filterCommands, insertCommandBody, slashQuery } from "../../slash-commands";
	import Icon from "../Icon.svelte";
	import AttachmentChips from "./AttachmentChips.svelte";
	import DesignChips from "./DesignChips.svelte";
	import SlashPalette from "./SlashPalette.svelte";

	let {
		disabled = false,
		running = false,
		value = $bindable(""),
		limits,
		onsubmit,
		oncancel,
	}: {
		disabled?: boolean;
		/** A turn is in flight; the send button becomes stop. */
		running?: boolean;
		/** Draft text — bindable so the greeting's suggestion chips can insert
		    text (TD-1605) without owning the textarea. */
		value?: string;
		/** The workspace's caps, from `boundary_update` (TD-1709). */
		limits: AttachmentLimits;
		onsubmit: (text: string, attachments: readonly AttachmentDraft[]) => void;
		oncancel?: () => void;
	} = $props();

	const MAX_ROWS = 8;

	let textarea: HTMLTextAreaElement | null = $state(null);
	let picker: HTMLInputElement | null = $state(null);
	let attachments: AttachmentDraft[] = $state([]);
	let refusal: AttachmentRefusal | null = $state(null);
	let dragging = $state(false);
	let nextAttachmentId = 0;
	let slashIndex = $state(0);
	let slashDismissed = $state(false);

	const query = $derived(slashQuery(value));
	const paletteOpen = $derived(query !== null && !slashDismissed);
	const visibleCommands = $derived(query === null ? [] : filterCommands(commands.items, query));

	$effect(() => {
		void visibleCommands.length;
		slashIndex = 0;
	});

	let lastQuery: string | null = null;
	$effect(() => {
		if (query !== lastQuery) {
			lastQuery = query;
			slashDismissed = false;
		}
	});

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

	/** Vet each file against the caps and the text test, one at a time so the
	    running total counts what earlier files in the same drop already took.
	    The first refusal stops the batch and is what the user is told: naming
	    one file and its fix beats a list nobody reads. */
	async function addFiles(files: readonly File[]): Promise<void> {
		if (disabled) return;
		refusal = null;
		for (const file of files) {
			const bytes = new Uint8Array(await file.arrayBuffer());
			const outcome = acceptAttachment(file.name, bytes, limits, attachments);
			if (!outcome.ok) {
				refusal = outcome.refusal;
				return;
			}
			nextAttachmentId += 1;
			attachments = [...attachments, { ...outcome.draft, id: `a${nextAttachmentId}` }];
		}
	}

	function removeAttachment(index: number): void {
		attachments = attachments.filter((_, i) => i !== index);
		refusal = null;
	}

	function handlePick(event: Event): void {
		const input = event.currentTarget as HTMLInputElement;
		void addFiles([...(input.files ?? [])]);
		// Reset so picking the same file twice in a row still fires a change.
		input.value = "";
	}

	function handleDrop(event: DragEvent): void {
		dragging = false;
		const files = [...(event.dataTransfer?.files ?? [])];
		if (files.length === 0) return;
		event.preventDefault();
		void addFiles(files);
	}

	function handlePaste(event: ClipboardEvent): void {
		const files = [...(event.clipboardData?.files ?? [])];
		// No files on the clipboard means an ordinary text paste; leave it be.
		if (files.length === 0) return;
		event.preventDefault();
		void addFiles(files);
	}

	// A running turn no longer refuses the submit (TD-1704): the store parks
	// the text as a queued row instead. The button still morphs to stop, so
	// while a turn runs Enter is the way in — the queued row above the card
	// is the confirmation that it landed.
	function submit(): void {
		const text = value.trim();
		const picks = acceptPickDrafts(design.picks, limits, attachments);
		if (!picks.ok) {
			refusal = { name: "design-pick.json", code: "attachment_too_many", message: picks.message };
			return;
		}
		const outgoing: AttachmentDraft[] = [
			...attachments,
			...picks.drafts.map((d, i) => ({ ...d, id: `d${i}` })),
		];
		// TD-1709: attached files alone are a message worth sending.
		if ((text === "" && outgoing.length === 0) || disabled) return;
		onsubmit(text, outgoing);
		value = "";
		attachments = [];
		clearPicks();
		refusal = null;
	}

	function applyCommand(command: CommandEntry, send: boolean): void {
		if (command.too_large) return;
		value = insertCommandBody(value, command.body);
		if (send) submit();
	}

	function handleFocus(): void {
		const path = session.workspacePath;
		if (path !== null) loadCommands(path);
	}

	function handleKeydown(event: KeyboardEvent): void {
		if (paletteOpen) {
			if (event.key === "ArrowDown") {
				event.preventDefault();
				if (visibleCommands.length === 0) return;
				slashIndex = (slashIndex + 1) % visibleCommands.length;
				return;
			}
			if (event.key === "ArrowUp") {
				event.preventDefault();
				if (visibleCommands.length === 0) return;
				slashIndex = (slashIndex - 1 + visibleCommands.length) % visibleCommands.length;
				return;
			}
			if (event.key === "Escape") {
				event.preventDefault();
				slashDismissed = true;
				return;
			}
			if (event.key === "Enter" && !event.shiftKey) {
				event.preventDefault();
				const chosen = visibleCommands[slashIndex];
				if (chosen === undefined) return;
				applyCommand(chosen, event.metaKey || event.ctrlKey);
				return;
			}
		}
		if (shouldSubmit(event.key, event.shiftKey)) {
			event.preventDefault();
			submit();
		}
	}
</script>

<div class="composer">
	<!-- role/label so the drop target is announced, not just visible: the
	     textarea's own label says what to type, this one says what can be
	     dropped. -->
	<div
		class="card"
		role="group"
		aria-label="Message composer — drop text files here to attach them"
		class:dragging
		ondragover={(e) => {
			e.preventDefault();
			dragging = true;
		}}
		ondragleave={() => (dragging = false)}
		ondrop={handleDrop}
	>
		{#if design.picks.length > 0}
			<DesignChips picks={design.picks} onremove={removePick} />
		{/if}
		{#if attachments.length > 0}
			<AttachmentChips
				chips={toChips(attachments)}
				label="Attached files"
				onremove={removeAttachment}
			/>
		{/if}
		{#if paletteOpen}
			<SlashPalette
				items={visibleCommands}
				selectedIndex={slashIndex}
				oninsert={(command) => applyCommand(command, false)}
				onsend={(command) => applyCommand(command, true)}
				onhover={(index) => (slashIndex = index)}
			/>
		{/if}
		<div class="row">
			<textarea
				bind:this={textarea}
				bind:value
				rows="1"
				{disabled}
				placeholder={disabled ? "Waiting for a session…" : "Message the agent…"}
				aria-label="Message composer"
				aria-expanded={paletteOpen}
				aria-controls={paletteOpen ? "slash-list" : undefined}
				aria-autocomplete="list"
				onkeydown={handleKeydown}
				onpaste={handlePaste}
				onfocus={handleFocus}
			></textarea>
			<input
				bind:this={picker}
				type="file"
				multiple
				class="picker"
				tabindex="-1"
				aria-hidden="true"
				onchange={handlePick}
			/>
			<button
				type="button"
				class="attach"
				{disabled}
				title="Attach text files"
				onclick={() => picker?.click()}
				aria-label="Attach text files"
			>
				<Icon name="paperclip" size={16} />
			</button>
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
	</div>
	{#if refusal !== null}
		<p class="refusal" role="alert">{refusal.message}</p>
	{:else}
		<p class="disclaimer">TST Desk can make mistakes — check its work.</p>
	{/if}
</div>

<style>
	.composer {
		padding: var(--space-2) var(--space-4) var(--space-3);
	}

	/* The card carries the chrome; the textarea inside is chromeless. */
	.card {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		padding: var(--space-2) var(--space-2) var(--space-2) var(--space-4);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-xl);
		box-shadow: var(--shadow-sm);
		transition:
			border-color var(--transition-fast),
			background var(--transition-fast);
	}

	.card:focus-within {
		border-color: var(--color-accent);
	}

	/* A file is over the card: say so before it lands. */
	.card.dragging {
		border-color: var(--color-accent);
		background: var(--color-sunken);
	}

	.row {
		display: flex;
		align-items: flex-end;
		gap: var(--space-2);
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

	/* The native control is never shown; the paperclip drives it. */
	.picker {
		display: none;
	}

	.attach {
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

	.attach:hover:not(:disabled) {
		color: var(--color-ink);
		background: var(--color-sunken);
	}

	.attach:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
	}

	.attach:disabled {
		opacity: 0.5;
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

	/* Takes the disclaimer's slot rather than adding one, so a refusal never
	   nudges the card. */
	.refusal {
		margin: 0;
		padding-top: var(--space-2);
		font-size: var(--text-xs);
		text-align: center;
		color: var(--color-warn);
	}
</style>
