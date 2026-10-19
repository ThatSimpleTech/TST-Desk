<script lang="ts">
	// Message composer (TD-1004, restyled TD-1604): a lifted card — ≈24px
	// radius, hairline border, whisper shadow — holding the textarea and one
	// circular accent button that morphs send → stop while a turn runs
	// (Esc cancels too, TD-1609). Enter submits, Shift+Enter newlines,
	// auto-grows to eight rows before scrolling internally.
	//
	// The draft fills the card. Under it, a toolbar keeps Plan and the model
	// dropdowns on the left and Send on the right — the same split Cursor
	// uses, so the message is never squeezed beside the controls.
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
	import { capturePhoto, captureRoute, photoName } from "../../camera";
	import { detectPlatform } from "../../sent-files";
	import { COMPOSER_LINE_PADDING } from "../../composer-layout";
	import { commands, loadCommands } from "../../commands.svelte.js";
	import { acceptPickDrafts } from "../../design";
	import { clearPicks, design, removePick } from "../../design.svelte.js";
	import type { AttachmentLimits } from "../../protocol";
	import { grok, matchingGrokCommands, runGrokCommand } from "../../grok.svelte.js";
	import { localWorkspacePath, session } from "../../session-status.svelte.js";
	import {
		BUILTIN_SLASH_ITEMS,
		filterCommands,
		insertCommandBody,
		mergeSlashItems,
		slashQuery,
		type SlashItem,
	} from "../../slash-commands";
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
	let picker: HTMLInputElement | null = $state(null);
	let capturePicker: HTMLInputElement | null = $state(null);
	let attachments: AttachmentDraft[] = $state([]);
	let refusal: AttachmentRefusal | null = $state(null);
	let dragging = $state(false);
	let nextAttachmentId = 0;
	let slashIndex = $state(0);
	let slashDismissed = $state(false);

	const query = $derived(slashQuery(value));
	const paletteOpen = $derived(query !== null && !slashDismissed);
	// Workspace commands are read from this computer's disk: they belong to
	// the session only when its workspace is local (TD-5015). Built-ins
	// (TD-5109) belong to the native loop; Grok has its own.
	const workspaceCommands = $derived(
		localWorkspacePath() !== null && commands.workspacePath === localWorkspacePath() ? commands.items : [],
	);
	const slashItems = $derived(
		mergeSlashItems(
			workspaceCommands,
			stack.skills,
			session.engine === "grok" ? [] : BUILTIN_SLASH_ITEMS,
		),
	);
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

	// Re-measure on every edit; cap growth at MAX_ROWS lines.
	$effect(() => {
		void value;
		const box = textarea;
		if (box === null) return;
		const computed = parseFloat(getComputedStyle(box).lineHeight);
		const lineHeight = Number.isFinite(computed) ? computed : 24;
		const maxHeight = lineHeight * MAX_ROWS + COMPOSER_LINE_PADDING;
		box.style.height = "auto";
		box.style.height = `${Math.min(box.scrollHeight, maxHeight)}px`;
	});

	/** Vet each file against the caps and the text test, one at a time so the
	    running total counts what earlier files in the same drop already took.
	    The first refusal stops the batch and is what the user is told: naming
	    one file and its fix beats a list nobody reads. */
	async function addFiles(files: readonly File[]): Promise<void> {
		const read: { name: string; bytes: Uint8Array }[] = [];
		for (const file of files) read.push({ name: file.name, bytes: new Uint8Array(await file.arrayBuffer()) });
		await addBytes(read);
	}

	/** The same gate as addFiles, for bytes that never were a File — a camera
	    frame arrives as bytes already (a File is just bytes with a name). */
	async function addBytes(files: readonly { name: string; bytes: Uint8Array }[]): Promise<void> {
		if (disabled) return;
		refusal = null;
		for (const file of files) {
			const outcome = acceptAttachment(file.name, file.bytes, limits, attachments, allowImages);
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
		attachMenuOpen = false;
	}

	// The paperclip opens a two-item menu: pick a file, or take a photo. The
	// photo path opens the camera, grabs one frame as PNG, and hands the bytes
	// to the same attachment gate as a picked file — the caps do not care where
	// the bytes came from, and a refusal here is the same courtesy copy.
	let attachMenuOpen = $state(false);
	let cameraBusy = $state(false);

	// Close the menu when focus or a click lands outside it. The rail's menus
	// close on toggle alone; this one is anchored over the toolbar, so a click
	// elsewhere needs to dismiss it too.
	$effect(() => {
		if (!attachMenuOpen) return;
		const onPointerDown = (event: PointerEvent): void => {
			if (!event.composedPath().some((el) => el instanceof HTMLElement && el.classList.contains("attach-wrap"))) {
				attachMenuOpen = false;
			}
		};
		const onKey = (event: KeyboardEvent): void => {
			if (event.key === "Escape") attachMenuOpen = false;
		};
		document.addEventListener("pointerdown", onPointerDown);
		document.addEventListener("keydown", onKey);
		return () => {
			document.removeEventListener("pointerdown", onPointerDown);
			document.removeEventListener("keydown", onKey);
		};
	});

	async function takePhoto(): Promise<void> {
		attachMenuOpen = false;
		if (cameraBusy) return;
		// On Android the camera *app* opens and the shot comes back through the
		// file-input path; getUserMedia there would capture silently without
		// ever opening the camera UI. Everywhere else (desktop) capture in-page.
		if (captureRoute(detectPlatform()) === "camera-app") {
			capturePicker?.click();
			return;
		}
		cameraBusy = true;
		try {
			const outcome = await capturePhoto();
			if (!outcome.ok) {
				refusal = { name: photoName(), code: outcome.failure.code, message: outcome.failure.message };
				return;
			}
			await addBytes([{ name: photoName(), bytes: outcome.bytes }]);
		} finally {
			cameraBusy = false;
		}
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

	function applyCommand(command: SlashItem, send: boolean): void {
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
		<div class="entry" data-model-placement="footer">
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
			<div class="bar">
				<div class="model-slot">
					<ModelControls />
				</div>
				<div class="actions">
					<DictationButton {disabled} ontranscript={appendDictation} />
					<div class="attach-wrap">
						<input
							bind:this={picker}
							type="file"
							multiple
							class="picker"
							tabindex="-1"
							aria-hidden="true"
							onchange={handlePick}
						/>
						<!-- Camera-app route (Android): `capture` makes the shell launch the
						     camera instead of the picker; the shot returns on the same
						     change path. Ignored on desktop, where this input is never used. -->
						<input
							bind:this={capturePicker}
							type="file"
							accept="image/png,image/jpeg"
							capture="environment"
							class="picker"
							tabindex="-1"
							aria-hidden="true"
							onchange={handlePick}
						/>
						<button
							type="button"
							class="attach"
							{disabled}
							title="Attach a file or take a photo"
							aria-label="Attach a file or take a photo"
							aria-haspopup="menu"
							aria-expanded={attachMenuOpen}
							onclick={() => (attachMenuOpen = !attachMenuOpen)}
						>
							<Icon name="paperclip" size={16} />
						</button>
						{#if attachMenuOpen}
							<div class="attach-menu" role="menu" aria-label="Add attachment">
								<button
									type="button"
									class="attach-item"
									role="menuitem"
									onclick={() => {
										attachMenuOpen = false;
										picker?.click();
									}}
								>
									<Icon name="file" size={14} />
									<span>Upload files</span>
								</button>
								<button
									type="button"
									class="attach-item"
									role="menuitem"
									disabled={cameraBusy}
									onclick={() => void takePhoto()}
								>
									<Icon name="camera" size={14} />
									<span>{cameraBusy ? "Taking a photo…" : "Take a photo"}</span>
								</button>
							</div>
						{/if}
					</div>
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
		padding: var(--space-3) var(--space-3) var(--space-2);
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
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		min-width: 0;
	}

	.draft {
		min-width: 0;
	}

	/* Text above, choices on the left, send on the right. The attach menu
	   positions against this card on narrow screens. */
	.bar {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
		min-width: 0;
	}

	.model-slot {
		display: flex;
		align-items: center;
		min-width: 0;
		flex: 1;
	}

	.model-slot:empty {
		display: none;
	}

	.actions {
		display: flex;
		align-items: center;
		gap: var(--space-1);
		flex-shrink: 0;
		margin-left: auto;
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

	.attach-wrap {
		position: relative;
		display: inline-flex;
	}

	/* The two-item add menu: upload, or take a photo. Anchored above the
	   toolbar because the composer sits at the bottom edge of the window,
	   and anchored to the right so it opens leftward — the paperclip sits
	   at the right edge of the toolbar, and a left-anchored menu hangs off
	   the screen on narrow viewports. */
	.attach-menu {
		position: absolute;
		bottom: calc(100% + var(--space-1));
		right: 0;
		z-index: 10;
		display: flex;
		flex-direction: column;
		min-width: 160px;
		padding: var(--space-1);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
	}

	/* Narrow viewport (TD-3701 breakpoint): overlay to the centre of the
	   composer instead of hanging off the screen edge. Dropping the wrap's
	   anchor lets the menu position against the bar, which spans the card. */
	@media (max-width: 639px) {
		.attach-wrap {
			position: static;
		}
		.attach-menu {
			right: auto;
			left: 50%;
			transform: translateX(-50%);
		}
	}

	.attach-item {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-1) var(--space-2);
		font-size: var(--text-sm);
		text-align: left;
		color: var(--color-ink);
		background: transparent;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
	}

	.attach-item:hover:not(:disabled) {
		background: var(--color-sunken);
	}

	.attach-item:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: -1px;
	}

	.attach-item:disabled {
		opacity: 0.5;
		cursor: not-allowed;
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
