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
	import {
		matchCommands,
		matchSkills,
		requestCommands,
		slashCommands,
	} from "../../commands.svelte.js";
	import { acceptPickDrafts } from "../../design";
	import { clearPicks, design, removePick } from "../../design.svelte.js";
	import type { AttachmentLimits, CommandEntry, SkillSummary } from "../../protocol";
	import Icon from "../Icon.svelte";
	import AttachmentChips from "./AttachmentChips.svelte";
	import DesignChips from "./DesignChips.svelte";

	let {
		disabled = false,
		running = false,
		value = $bindable(""),
		limits,
		workspacePath = null,
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
		/** The open workspace, for the slash-command listing (TD-4501). */
		workspacePath?: string | null;
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

	// ── Slash commands and skills (TD-4501, TD-4502) ────────────────────
	//
	// Typing "/" opens a menu of the workspace's command files and skill
	// catalog. Enter or click inserts "/name " so arguments can follow
	// (default insert); Alt+Enter sends the invocation as typed — expansion
	// happens in the daemon either way. Not steering: the body splices only
	// when invoked. A skill row says what it is — choosing one sends its
	// body instead of registering a command.

	const SLASH_RE = /^\/([A-Za-z0-9_-]*)$/;

	/** One flattened row of the menu. Commands and skills differ in what
	 *  happens on send, not in how they're chosen. */
	type MenuRow =
		| { kind: "command"; entry: CommandEntry }
		| { kind: "skill"; entry: SkillSummary };

	let menuHighlight = $state(0);
	// Escape closes until the query goes away; without this the very next
	// keystroke would reopen the menu the user just dismissed.
	let escaped = $state(false);

	let slashQuery = $derived.by(() => {
		if (value === "") return null;
		const match = SLASH_RE.exec(value.trimStart());
		return match === null ? null : match[1];
	});

	let menuRows = $derived.by<MenuRow[]>(() => {
		if (slashQuery === null) return [];
		return [
			...matchCommands(slashCommands.items, slashQuery).map(
				(entry): MenuRow => ({ kind: "command", entry }),
			),
			...matchSkills(slashCommands.skills, slashQuery).map(
				(entry): MenuRow => ({ kind: "skill", entry }),
			),
		];
	});

	let menuOpen = $derived(
		slashQuery !== null && !escaped && menuRows.length > 0 && !disabled,
	);

	// The listing is never pushed and may predate an edit to a command
	// file, so each open asks again — one cheap directory read.
	$effect(() => {
		if (menuOpen) requestCommands(workspacePath ?? null);
	});

	// The query going away also clears an Escape, so dismissing the menu
	// for "/de" doesn't stick when the user starts a different command.
	$effect(() => {
		if (slashQuery === null) escaped = false;
	});

	function pickRow(row: MenuRow): void {
		value = `/${row.entry.name} `;
		menuHighlight = 0;
		textarea?.focus();
	}

	function handleMenuKeydown(event: KeyboardEvent): boolean {
		if (!menuOpen) return false;
		if (event.key === "ArrowDown" || event.key === "ArrowUp") {
			event.preventDefault();
			const delta = event.key === "ArrowDown" ? 1 : -1;
			const count = menuRows.length;
			menuHighlight = (menuHighlight + delta + count) % count;
			return true;
		}
		if (event.key === "Enter" && !event.altKey) {
			event.preventDefault();
			pickRow(menuRows[menuHighlight]);
			return true;
		}
		if (event.key === "Escape") {
			event.preventDefault();
			escaped = true;
			menuHighlight = 0;
			return true;
		}
		return false;
	}

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

	function handleKeydown(event: KeyboardEvent): void {
		if (handleMenuKeydown(event)) return;
		// Over an open menu, Alt+Enter sends the invocation as typed — the
		// "send" half of insert-or-send (insert is the default).
		if (shouldSubmit(event.key, event.shiftKey) || (event.key === "Enter" && event.altKey)) {
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
		{#if menuOpen}
			<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
			<ul
				class="slash-menu"
				role="listbox"
				aria-label="Slash commands and skills"
				id="slash-command-menu"
			>
				{#each menuRows as row, i (row.kind + ":" + row.entry.name)}
					<!-- svelte-ignore a11y_mouse_events_have_key_events -->
					<li role="presentation" onmouseenter={() => (menuHighlight = i)}>
						<button
							type="button"
							class="slash-item"
							class:active={i === menuHighlight}
							role="option"
							aria-selected={i === menuHighlight}
							title={row.kind === "command" ? row.entry.path : row.entry.description || row.entry.name}
							onclick={() => pickRow(row)}
						>
							<span class="slash-name">/{row.entry.name}</span>
							<span class="slash-source">
								{#if row.kind === "skill"}
									skill · {row.entry.fallback ? "claude" : row.entry.source}
								{:else}
									{row.entry.fallback ? "claude" : row.entry.source}
								{/if}
							</span>
						</button>
					</li>
				{/each}
			</ul>
			<p class="slash-hint">Enter inserts · Alt+Enter sends</p>
		{/if}
		<div class="row">
			<textarea
				bind:this={textarea}
				bind:value
				rows="1"
				{disabled}
				placeholder={disabled ? "Waiting for a session…" : "Message the agent…"}
				aria-label="Message composer"
				// Combobox is the textbook role for "textbox with a popup", and
				// the only one under which aria-expanded/controls are legal.
				role="combobox"
				aria-autocomplete="list"
				aria-expanded={menuOpen}
				aria-controls={menuOpen ? "slash-command-menu" : undefined}
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

	/* The slash-command menu sits between the chips and the input row, so
	   the card grows upward around it instead of overlaying the page. */
	.slash-menu {
		margin: 0;
		padding: 0;
		list-style: none;
		border-bottom: 1px solid var(--color-hairline);
	}

	.slash-item {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		padding: var(--space-2) var(--space-2);
		background: transparent;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
		text-align: left;
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.slash-item.active,
	.slash-item:hover {
		background: var(--color-sunken);
	}

	.slash-name {
		font-family: var(--font-mono);
	}

	.slash-source {
		margin-left: auto;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.slash-hint {
		margin: 0;
		padding-top: var(--space-1);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
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
