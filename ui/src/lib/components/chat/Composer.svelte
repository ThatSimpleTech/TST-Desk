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
	import {
		commandMenu,
		ensureCommands,
		moveSelection,
		resetSelection,
		setSlashState,
	} from "../../commands-store.svelte.js";
	import { shouldSubmit } from "../../chat-store";
	import { acceptPickDrafts } from "../../design";
	import { clearPicks, design, removePick } from "../../design.svelte.js";
	import type { AttachmentLimits } from "../../protocol";
	import { insertCommand, rankCommands, slashQuery, sourceLabel } from "../../slash";
	import Icon from "../Icon.svelte";
	import AttachmentChips from "./AttachmentChips.svelte";
	import DesignChips from "./DesignChips.svelte";

	let {
		disabled = false,
		running = false,
		value = $bindable(""),
		limits,
		sessionId = null,
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
		/** Live session id, for fetching the slash listing (TD-4501). */
		sessionId?: string | null;
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

	// ── Slash commands (TD-4501) ──
	// A leading "/" with no whitespace after it is a query; the menu shows
	// the ranked matches unless Escape dismissed exactly this query.

	const slashQueryText = $derived(disabled ? null : slashQuery(value));
	const slashEntries = $derived(
		slashQueryText === null ? [] : rankCommands(commandMenu.commands, slashQueryText),
	);
	const slashOpen = $derived(
		slashQueryText !== null &&
			slashQueryText !== commandMenu.dismissedQuery &&
			slashEntries.length > 0,
	);

	// The listing rides one fetch per session; asking again is a no-op.
	$effect(() => {
		if (sessionId === null || sessionId === undefined) return;
		ensureCommands(sessionId);
	});

	// Keep the store's open/query mirror current — shortcuts.ts reads it to
	// give Escape its one opinion about this layer, so the composer installs
	// no keydown handler of its own for that key.
	$effect(() => {
		setSlashState(slashOpen, slashQueryText ?? "");
	});

	// A new query or listing puts the highlight back on the first row.
	$effect(() => {
		void slashQueryText;
		void slashEntries.length;
		resetSelection();
	});

	function chooseSlash(command: (typeof slashEntries)[number], send: boolean): void {
		value = insertCommand(command);
		if (!send) return;
		submit();
	}

	function chooseSelected(send: boolean): void {
		const command = slashEntries[commandMenu.selected] ?? slashEntries[0];
		if (command === undefined) return;
		chooseSlash(command, send);
	}

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

	function handleKeydown(event: KeyboardEvent): void {
		// The menu eats the navigation keys first (TD-4501); Escape is
		// deliberately not here — shortcuts.ts owns that layer.
		if (slashOpen) {
			if (event.key === "ArrowDown") {
				event.preventDefault();
				moveSelection(1, slashEntries.length);
				return;
			}
			if (event.key === "ArrowUp") {
				event.preventDefault();
				moveSelection(-1, slashEntries.length);
				return;
			}
			if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey)) {
				// Enter inserts (the default, per the story); ⌘/Ctrl+Enter sends
				// the chosen command straight away. Shift+Enter still newlines —
				// the menu stands aside for it.
				event.preventDefault();
				chooseSelected(event.metaKey || event.ctrlKey);
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
		<!-- TD-4501: the slash menu overlays upward from the card; mousedown is
		     swallowed so choosing with the mouse never steals the caret. -->
		{#if slashOpen}
			<ul class="slash-menu" id="slash-menu" role="listbox" aria-label="Slash commands">
				{#each slashEntries as command, i (command.name + command.source)}
					<li role="presentation">
						<button
							type="button"
							class="slash-option"
							class:selected={i === commandMenu.selected}
							role="option"
							id={`slash-option-${i}`}
							aria-selected={i === commandMenu.selected}
							onmousedown={(e) => e.preventDefault()}
							onclick={() => chooseSlash(command, false)}
						>
							<span class="slash-name">/{command.name}</span>
							{#if command.description !== null && command.description !== undefined}
								<span class="slash-desc">{command.description}</span>
							{/if}
							<span class="slash-source">{sourceLabel(command.source)}</span>
						</button>
					</li>
				{/each}
			</ul>
		{/if}
		<div class="row">
			<textarea
				bind:this={textarea}
				bind:value
				rows="1"
				{disabled}
				placeholder={disabled ? "Waiting for a session…" : "Message the agent…"}
				aria-label="Message composer"
				role={slashOpen ? "combobox" : undefined}
				aria-expanded={slashOpen ? "true" : undefined}
				aria-controls={slashOpen ? "slash-menu" : undefined}
				aria-activedescendant={
					slashOpen ? `slash-option-${commandMenu.selected}` : undefined
				}
				onkeydown={handleKeydown}
				onpaste={handlePaste}
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

	/* The card carries the chrome; the textarea inside is chromeless.
	   position:relative anchors the slash menu's upward overlay (TD-4501). */
	.card {
		position: relative;
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

	.slash-menu {
		position: absolute;
		left: 0;
		right: 0;
		bottom: calc(100% + var(--space-1));
		margin: 0;
		padding: var(--space-1);
		display: flex;
		flex-direction: column;
		gap: 2px;
		max-height: 300px;
		overflow-y: auto;
		list-style: none;
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-md);
		z-index: 10;
	}

	.slash-option {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		width: 100%;
		padding: var(--space-2) var(--space-3);
		text-align: left;
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: transparent;
		border: none;
		border-radius: var(--radius-md);
		cursor: pointer;
	}

	.slash-option.selected,
	.slash-option:hover {
		background: var(--color-sunken);
	}

	.slash-name {
		font-family: var(--font-mono, monospace);
		flex-shrink: 0;
	}

	/* The description takes the middle and pushes the source tag right; long
	   ones ellipsize rather than wrap the row tall. */
	.slash-desc {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--color-ink-secondary);
	}

	.slash-source {
		flex-shrink: 0;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
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
