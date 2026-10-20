<script lang="ts">
	// A file's latest diff that takes line comments (TD-5132).
	//
	// Same look as DiffPreview, plus a gutter of line numbers for the file
	// after this write. Click a number to comment on that line; shift-click a
	// second one for a range. Removed lines have no number in the new file,
	// so they take no comment. Drafts show under the line they end on, ready
	// to send with every other draft from the review bar.
	import {
		MAX_COMMENT_LINES,
		numberDiffRows,
		rangeLines,
		spanLabel,
		type DraftComment
	} from '../review-comments';
	import { addDraft, removeDraft, review } from '../review-comments.svelte.js';

	let { path, diff, seq = null }: { path: string; diff: string; seq?: number | null } = $props();

	let rows = $derived(numberDiffRows(diff));
	let drafts = $derived(review.drafts.filter((d) => d.path === path));
	let anchor = $state<number | null>(null);
	let selection = $state<{ start: number; end: number } | null>(null);
	let body = $state('');
	let problem = $state<string | null>(null);

	function pick(line: number, extend: boolean): void {
		problem = null;
		if (extend && anchor !== null) {
			selection = { start: Math.min(anchor, line), end: Math.max(anchor, line) };
		} else {
			anchor = line;
			selection = { start: line, end: line };
		}
	}

	function cancel(): void {
		anchor = null;
		selection = null;
		body = '';
		problem = null;
	}

	function save(): void {
		if (selection === null) return;
		const { start, end } = selection;
		if (end - start + 1 > MAX_COMMENT_LINES) {
			problem = `A comment can cover at most ${MAX_COMMENT_LINES} lines.`;
			return;
		}
		const lines = rangeLines(rows, start, end);
		if (lines === null) {
			problem = 'That range skips lines this diff does not show. Pick lines from one block.';
			return;
		}
		if (!addDraft(path, start, end, lines, body, seq)) {
			problem = 'Write the comment first.';
			return;
		}
		cancel();
	}

	function inDraft(line: number | null, list: DraftComment[]): boolean {
		return line !== null && list.some((d) => line >= d.start_line && line <= d.end_line);
	}

	function selected(line: number | null): boolean {
		return line !== null && selection !== null && line >= selection.start && line <= selection.end;
	}
</script>

<div class="diff" role="group" aria-label={`Diff with line comments for ${path}`}>
	{#each rows as row, i (i)}
		<div
			class="line line--{row.kind}"
			class:line--selected={selected(row.newLine)}
			class:line--drafted={inDraft(row.newLine, drafts)}
		>
			{#if row.newLine !== null}
				<button
					class="gutter"
					type="button"
					aria-label={`Comment on line ${row.newLine}`}
					aria-pressed={selected(row.newLine)}
					onclick={(e) => pick(row.newLine as number, e.shiftKey)}>{row.newLine}</button
				>
			{:else}
				<span class="gutter gutter--blank" aria-hidden="true"></span>
			{/if}
			<span class="code">{row.line}</span>
		</div>
		{#if row.newLine !== null && selection !== null && row.newLine === selection.end}
			<div class="editor">
				<label class="editor-label" for={`comment-${path}`}
					>Comment on {spanLabel(selection.start, selection.end)}
					<span class="hint">· shift-click a number for a range</span></label
				>
				<textarea
					id={`comment-${path}`}
					class="editor-input"
					rows="3"
					bind:value={body}
					placeholder="What should the agent change here?"
				></textarea>
				{#if problem}<p class="problem" role="alert">{problem}</p>{/if}
				<div class="editor-actions">
					<button type="button" class="btn" onclick={cancel}>Cancel</button>
					<button type="button" class="btn btn--primary" onclick={save}>Add comment</button>
				</div>
			</div>
		{/if}
		{#each drafts.filter((d) => row.newLine !== null && d.end_line === row.newLine) as draft (draft.id)}
			<div class="draft">
				<span class="draft-span">{spanLabel(draft.start_line, draft.end_line)}</span>
				<span class="draft-body">{draft.body}</span>
				<button
					type="button"
					class="btn btn--quiet"
					aria-label="Remove comment"
					onclick={() => removeDraft(draft.id)}>Remove</button
				>
			</div>
		{/each}
	{/each}
</div>

<style>
	.diff {
		margin: 0;
		padding: var(--space-1) 0;
		background: var(--color-ground);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		overflow-x: auto;
	}

	.line {
		display: flex;
		min-width: max-content;
		color: var(--color-ink-secondary);
	}

	.code {
		padding: 0 var(--space-3);
		white-space: pre;
	}

	.code:empty::after {
		content: '\00a0';
	}

	.gutter {
		flex-shrink: 0;
		width: 3.5em;
		padding: 0 var(--space-1);
		border: none;
		border-right: var(--border-width) solid var(--color-hairline);
		background: none;
		color: var(--color-ink-muted);
		font: inherit;
		text-align: right;
		cursor: pointer;
	}

	.gutter--blank {
		display: inline-block;
		cursor: default;
	}

	.gutter:hover:not(:disabled),
	.gutter:focus-visible {
		color: var(--color-accent);
		background: var(--color-sunken);
	}

	.gutter:disabled {
		cursor: default;
	}

	.line--add {
		color: var(--color-ok);
		background: color-mix(in srgb, var(--color-ok) 12%, transparent);
	}

	.line--del {
		color: var(--color-err);
		background: color-mix(in srgb, var(--color-err) 12%, transparent);
	}

	.line--hunk {
		color: var(--color-accent);
	}

	.line--meta {
		color: var(--color-ink-muted);
		font-weight: var(--weight-semibold);
	}

	.line--drafted .gutter {
		color: var(--color-accent);
		font-weight: var(--weight-semibold);
	}

	.line--selected {
		box-shadow: inset 3px 0 0 var(--color-accent);
		background: color-mix(in srgb, var(--color-accent) 14%, transparent);
	}

	.editor,
	.draft {
		margin: var(--space-1) var(--space-2);
		padding: var(--space-2);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		background: var(--color-lifted);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.editor {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.editor-label {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-ink-secondary);
	}

	.hint {
		font-weight: normal;
		color: var(--color-ink-muted);
	}

	.editor-input {
		width: 100%;
		box-sizing: border-box;
		padding: var(--space-2);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		background: var(--color-ground);
		color: var(--color-ink);
		font: inherit;
		resize: vertical;
	}

	.problem {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-err);
	}

	.editor-actions {
		display: flex;
		justify-content: flex-end;
		gap: var(--space-2);
	}

	.draft {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
	}

	.draft-span {
		flex-shrink: 0;
		font-size: var(--text-xs);
		color: var(--color-accent);
		font-weight: var(--weight-semibold);
	}

	.draft-body {
		flex: 1;
		min-width: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.btn {
		padding: var(--space-1) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		background: var(--color-ground);
		color: var(--color-ink);
		font-size: var(--text-xs);
		cursor: pointer;
	}

	.btn--primary {
		border-color: var(--color-accent);
		background: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn--quiet {
		border-color: transparent;
		background: none;
		color: var(--color-ink-muted);
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
</style>
