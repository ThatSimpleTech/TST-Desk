<script lang="ts">
	// "Restore files to here" (TD-5125). Shows the files the agent changed
	// after a message and the diff a restore takes away, before anything is
	// written. Files changed by hand since are listed apart, unticked: they
	// stay as they are unless the person ticks them. Edit and resend comes
	// through here too, and resends once the files are back; if the restore is
	// refused or cancelled, the edited text goes back to its row.
	import {
		closeRestore,
		confirmRestore,
		resendWithoutRestoring,
		rewind,
		toggleHandEdited,
	} from '../rewind-store.svelte.js';
	import { changeLabel, handEdited, restoreCount, restoredSummary } from '../rewind';
	import DiffPreview from './DiffPreview.svelte';

	let preview = $derived(rewind.preview);
	let files = $derived(preview?.files ?? []);
	let byHand = $derived(handEdited(preview));
	let count = $derived(restoreCount(rewind));
	let resending = $derived(rewind.resend !== null);

	function onKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		event.stopPropagation();
		closeRestore();
	}
</script>

{#if rewind.phase !== 'closed'}
	<div
		class="overlay"
		role="dialog"
		tabindex="-1"
		aria-modal="true"
		aria-label="Restore files to here"
		onkeydown={onKeydown}
	>
		<div class="card">
			<h2 class="title">Restore files to here</h2>
			<p class="body">
				Puts back the files the agent changed after this message{resending
					? ', then resends your edited message'
					: '. The conversation stays as it is'}.
			</p>
			<p class="note">
				Only the agent's file edits are tracked. Anything a shell command changed stays as it is.
			</p>

			{#if rewind.phase === 'loading'}
				<p class="body" role="status">Looking at what the agent changed…</p>
				<div class="actions">
					<button class="btn" type="button" onclick={closeRestore}>Cancel</button>
				</div>
			{:else if rewind.phase === 'error'}
				<p class="error" role="alert">{rewind.error}</p>
				{#if resending}
					<p class="body">
						Your edited message is kept. Close goes back to it; you can also send it without
						restoring.
					</p>
				{/if}
				<div class="actions">
					<button class="btn" type="button" onclick={closeRestore}>Close</button>
					{#if resending}
						<button class="btn btn--primary" type="button" onclick={resendWithoutRestoring}
							>Resend without restoring</button
						>
					{/if}
				</div>
			{:else if rewind.phase === 'done' && rewind.result !== null}
				<p class="done" role="status">
					{restoredSummary(rewind.result)}{rewind.resent
						? ' Your edited message was resent.'
						: resending
							? " Your edited message wasn't sent; it goes back to its edit box when you close this."
							: ''}
				</p>
				<div class="actions">
					<button class="btn btn--primary" type="button" onclick={closeRestore}>Done</button>
				</div>
			{:else if files.length === 0}
				<p class="body" role="status">The agent hasn't changed any files since this message.</p>
				<div class="actions">
					<button class="btn" type="button" onclick={closeRestore}>Close</button>
					{#if resending}
						<button class="btn btn--primary" type="button" onclick={resendWithoutRestoring}
							>Resend</button
						>
					{/if}
				</div>
			{:else}
				<ul class="files" aria-label="Files to restore">
					{#each files as file (file.path)}
						<li class="file" class:file--hand={file.hand_edited}>
							{#if file.hand_edited}
								<label class="tick">
									<input
										type="checkbox"
										checked={rewind.ticked.includes(file.path)}
										onchange={() => toggleHandEdited(file.path)}
									/>
									<span class="path">{file.path}</span>
								</label>
								<span class="note">Changed by hand since. Left as it is unless ticked.</span>
							{:else}
								<span class="path">{file.path}</span>
								<span class="note">{changeLabel(file.change)}</span>
							{/if}
						</li>
					{/each}
				</ul>
				{#if byHand.length > 0}
					<p class="warn" role="note">
						{byHand.length === 1 ? '1 file was' : `${byHand.length} files were`} changed by hand
						after the agent's edits. Ticking one throws those changes away too.
					</p>
				{/if}
				<h3 class="sub">Changes to undo</h3>
				{#if preview !== null && preview.diff !== ''}
					<div class="diff"><DiffPreview diff={preview.diff} /></div>
				{/if}
				{#if preview?.diff_truncated}
					<p class="body">The diff is cut short here; every file above is still restored.</p>
				{/if}
				<div class="actions">
					<button class="btn" type="button" onclick={closeRestore}>Cancel</button>
					{#if resending}
						<button class="btn" type="button" onclick={resendWithoutRestoring}
							>Resend without restoring</button
						>
					{/if}
					<button
						class="btn btn--primary"
						type="button"
						disabled={rewind.phase === 'restoring' || count === 0}
						onclick={confirmRestore}
						>{rewind.phase === 'restoring'
							? 'Restoring…'
							: resending
								? `Restore ${count === 1 ? '1 file' : `${count} files`} and resend`
								: `Restore ${count === 1 ? '1 file' : `${count} files`}`}</button
					>
				</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.overlay {
		position: fixed;
		inset: 0;
		z-index: var(--z-modal);
		display: grid;
		place-items: center;
		padding: var(--space-4);
		background: rgb(0 0 0 / 0.28);
	}

	.card {
		width: min(44rem, 100%);
		max-height: 90vh;
		overflow-y: auto;
		background: var(--color-lifted);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-lg);
		padding: var(--space-5);
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.title {
		margin: 0;
		font-size: var(--text-lg);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}

	.sub {
		margin: 0;
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}

	.body,
	.warn,
	.note {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		line-height: 1.5;
	}

	.warn {
		color: var(--color-warn);
	}

	.note {
		font-size: var(--text-xs);
	}

	.files {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.file {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		column-gap: var(--space-2);
	}

	.file--hand .path {
		color: var(--color-warn);
	}

	.tick {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
	}

	.path {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink);
		word-break: break-all;
	}

	.diff {
		max-width: 100%;
		overflow-x: auto;
	}

	.done {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ok);
	}

	.error {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-err);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		justify-content: flex-end;
	}

	.btn {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-3);
		border-radius: var(--radius-md);
		border: 1px solid var(--color-hairline);
		background: transparent;
		color: var(--color-ink);
		cursor: pointer;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.btn--primary {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn--primary:hover:not(:disabled) {
		background: var(--color-accent-hover);
	}
</style>
