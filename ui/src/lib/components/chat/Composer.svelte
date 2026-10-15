<script lang="ts">
	// Message composer (TD-1004, restyled TD-1604): a lifted card — ≈24px
	// radius, hairline border, whisper shadow — holding the textarea and one
	// circular accent button that morphs send → stop while a turn runs
	// (Esc cancels too, TD-1609). Enter submits, Shift+Enter newlines,
	// auto-grows to eight rows before scrolling internally.
	//
	// Plan, the tier picker, and the session preset sit to the right of Send
	// while the draft is one line, and drop to a footer under the text once
	// that line would wrap beside them.
	//
	// Text files and PNG/JPEG/GIF/WebP images attach here three ways
	// (TD-1709): the paperclip's picker, drag-and-drop onto the card, and
	// paste. The refusal shown inline is a courtesy — the daemon refuses
	// the same file again on arrival, and that is the gate that actually
	// holds. Deliberately no `accept` filter on the picker: a file the
	// user cannot even select produces no copy explaining why, and the
	// copy is the point.
	import {
		acceptAttachment,
		toChips,
		type AttachmentDraft,
		type AttachmentRefusal,
	} from "../../attachments";
	import { shouldSubmit } from "../../chat-store";
	import {
		COMPOSER_LINE_PADDING,
		inlineDraftWidth,
		modelControlsInFooter,
	} from "../../composer-layout";
	import { commands, loadCommands } from "../../commands.svelte.js";
	import { acceptPickDrafts } from "../../design";
	import { clearPicks, design, removePick } from "../../design.svelte.js";
	import type { AttachmentLimits, CommandEntry } from "../../protocol";
	import { grok, matchingGrokCommands, runGrokCommand } from "../../grok.svelte.js";
	import { localWorkspacePath, session } from "../../session-status.svelte.js";
	import { filterCommands, insertCommandBody, mergeSlashItems, slashQuery } from "../../slash-commands";
	import { stack } from "../../stack-store.svelte.js";
	import Icon from "../Icon.svelte";
	import AttachmentChips from "./AttachmentChips.svelte";
	import DesignChips from "./DesignChips.svelte";
	import DictationButton from "./DictationButton.svelte";
	import ModelControls from "./ModelControls.svelte";
	import SlashPalette from "./SlashPalette.svelte";

	let {
		disabled = false,
		running = false,
		value = $bindable(""),
		limits,
		allowImages = false,
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
		/** Active tier accepts images (TD-4705), from `tier_state`. */
		allowImages?: boolean;
		onsubmit: (text: string, attachments: readonly AttachmentDraft[]) => void;
		oncancel?: () => void;
	} = $props();

	const MAX_ROWS = 8;

	let textarea: HTMLTextAreaElement | null = $state(null);
	let entryEl: HTMLElement | null = $state(null);
	let actionsEl: HTMLElement | null = $state(null);
	let modelEl: HTMLElement | null = $state(null);
	let measure: HTMLElement | null = $state(null);
	/** Model controls have left the send row so the draft can use its width. */
	let footer = $state(false);
	let picker: HTMLInputElement | null = $state(null);
	let attachments: AttachmentDraft[] = $state([]);
	let refusal: AttachmentRefusal | null = $state(null);
	let dragging = $state(false);
	let nextAttachmentId = 0;
	let slashIndex = $state(0);
	let slashDismissed = $state(false);

	const query = $derived(slashQuery(value));
	const paletteOpen = $derived(query !== null && !slashDismissed);
	// Workspace commands are read from this computer's disk: they belong to
	// the session only when its workspace is local (TD-5015).
	const workspaceCommands = $derived(
		localWorkspacePath() !== null && commands.workspacePath === localWorkspacePath() ? commands.items : [],
	);
	const slashItems = $derived(mergeSlashItems(workspaceCommands, stack.skills));
	const visibleCommands = $derived(query === null ? [] : filterCommands(slashItems, query));

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

	// Re-measure on every edit; cap growth at MAX_ROWS lines. The footer
	// decision uses the width the draft would have with the controls still
	// beside Send, so moving them does not change the answer.
	$effect(() => {
		void value;
		const box = textarea;
		const entry = entryEl;
		const actions = actionsEl;
		const model = modelEl;
		const mirror = measure;
		if (box === null || entry === null || actions === null || model === null || mirror === null) return;

		let applying = false;
		const apply = (): void => {
			if (applying) return;
			applying = true;
			try {
				const computed = parseFloat(getComputedStyle(box).lineHeight);
				const lineHeight = Number.isFinite(computed) ? computed : 24;
				const maxHeight = lineHeight * MAX_ROWS + COMPOSER_LINE_PADDING;
				box.style.height = "auto";
				box.style.height = `${Math.min(box.scrollHeight, maxHeight)}px`;

				const rawGap = parseFloat(getComputedStyle(entry).columnGap);
				const gap = Number.isFinite(rawGap) ? rawGap : 8;
				// The slot stretches to the full row once it is a footer. The
				// controls themselves stay content-sized, which is the width
				// they take beside Send.
				const controls = model.querySelector(".controls");
				const inlineWidth = inlineDraftWidth(
					entry.clientWidth,
					actions.offsetWidth,
					controls?.getBoundingClientRect().width ?? 0,
					gap,
				);
				mirror.style.width = `${inlineWidth}px`;
				const next =
					controls !== null &&
					modelControlsInFooter(
						value,
						mirror.scrollHeight,
						lineHeight,
						inlineWidth,
						entry.clientWidth,
					);
				if (next !== footer) footer = next;
			} finally {
				applying = false;
			}
		};

		apply();
		if (typeof ResizeObserver === "undefined") return;
		const observer = new ResizeObserver(apply);
		observer.observe(entry);
		observer.observe(actions);
		observer.observe(model);
		return () => observer.disconnect();
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
			const outcome = acceptAttachment(file.name, bytes, limits, attachments, allowImages);
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

	let slashHits = $derived(value.startsWith("/") ? matchingGrokCommands(value, session.sessionId) : []);

	function pickSlash(name: string): void {
		if (session.sessionId === null) {
			value = `/${name} `;
			return;
		}
		const rest = value.replace(/^\/\S*\s*/, "");
		// The draft stays when nothing was sent.
		if (!runGrokCommand(session.sessionId, name, rest)) return;
		value = "";
		attachments = [];
		refusal = null;
	}

	function applyCommand(command: CommandEntry, send: boolean): void {
		if (command.too_large) return;
		value = insertCommandBody(value, command.body);
		if (send) submit();
	}

	function handleFocus(): void {
		const path = localWorkspacePath();
		if (path !== null) loadCommands(path);
	}

	function handleKeydown(event: KeyboardEvent): void {
		if (slashHits.length > 0 && event.key === "Tab") {
			event.preventDefault();
			pickSlash(slashHits[0].name);
			return;
		}
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

	function appendDictation(text: string): void {
		const piece = text.trim();
		if (!piece) return;
		const cur = value;
		if (!cur) {
			value = piece;
			return;
		}
		const sep = /[\s\n]$/.test(cur) ? "" : " ";
		value = `${cur}${sep}${piece}`;
	}
</script>

<div class="composer">
	{#if slashHits.length > 0 && grok.commands.length > 0}
		<ul class="slash" role="listbox" aria-label="Grok commands">
			{#each slashHits as cmd (cmd.name)}
				<li>
					<button type="button" onclick={() => pickSlash(cmd.name)}>
						<span class="slash-name">/{cmd.name}</span>
						<span class="slash-desc">{cmd.description}</span>
					</button>
				</li>
			{/each}
		</ul>
	{/if}
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
		<div
			class="entry"
			class:footer
			data-model-placement={footer ? "footer" : "inline"}
			bind:this={entryEl}
		>
			<div class="measure-clip" aria-hidden="true">
				<span class="measure" bind:this={measure}>{value || " "}</span>
			</div>
			<div class="draft">
				<textarea
					bind:this={textarea}
					bind:value
					rows="1"
					{disabled}
					placeholder={disabled ? "Waiting for a session…" : "Message the agent…"}
					aria-label="Message composer"
					aria-controls={paletteOpen ? "slash-list" : undefined}
					aria-autocomplete="list"
					onkeydown={handleKeydown}
					onpaste={handlePaste}
					onfocus={handleFocus}
				></textarea>
			</div>
			<div class="actions" bind:this={actionsEl}>
				<DictationButton {disabled} ontranscript={appendDictation} />
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
						title="Stop this turn (Esc)"
						onclick={() => oncancel?.()}
						aria-label="Stop this turn"
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
			<div class="model-slot" bind:this={modelEl}>
				<ModelControls />
			</div>
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

	.slash {
		list-style: none;
		margin: 0 0 var(--space-2);
		padding: 0;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-lifted);
		box-shadow: var(--shadow-md);
		animation: rise var(--dur-enter) var(--ease-out);
		max-height: 12rem;
		overflow-y: auto;
	}
	.slash button {
		display: flex;
		gap: var(--space-2);
		width: 100%;
		text-align: left;
		background: none;
		border: 0;
		color: inherit;
		padding: 6px 10px;
		cursor: pointer;
		font-size: var(--text-sm);
	}
	.slash-name {
		font-family: var(--font-mono);
	}
	.slash-desc {
		color: var(--color-ink-muted);
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

	.entry {
		position: relative;
		z-index: 1;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto auto;
		grid-template-areas: "draft actions model";
		align-items: end;
		column-gap: var(--space-2);
		row-gap: var(--space-2);
	}

	/* The controls leave the send row. The draft takes their column so the
	   message is not squeezed, and they become a bar under the text. */
	.entry.footer {
		grid-template-columns: minmax(0, 1fr) auto;
		grid-template-areas:
			"draft actions"
			"model model";
	}

	.measure-clip {
		position: absolute;
		width: 0;
		height: 0;
		overflow: hidden;
	}

	.measure {
		display: block;
		white-space: pre-wrap;
		font-family: var(--font-sans);
		font-size: var(--text-base);
		line-height: var(--leading-normal);
	}

	.draft {
		grid-area: draft;
		min-width: 0;
	}

	.actions {
		grid-area: actions;
		display: flex;
		align-items: center;
		gap: var(--space-2);
	}

	.model-slot {
		grid-area: model;
		display: flex;
		justify-content: flex-end;
		min-width: 0;
		max-width: 100%;
	}

	.model-slot:empty {
		display: none;
	}

	.entry.footer .model-slot {
		padding-top: var(--space-2);
		border-top: var(--border-width) solid var(--color-hairline);
	}

	textarea {
		width: 100%;
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
