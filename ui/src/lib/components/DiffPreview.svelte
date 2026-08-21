<script lang="ts">
	// Highlighted unified-diff body. Shared by Files (TD-1705) and Work (TD-3203)
	// so neither pane copies the line classifier or the token colors.
	import { classifyDiffLine, diffLines } from '../entry-view';

	let { diff }: { diff: string } = $props();
</script>

<pre class="code diff">{#each diffLines(diff) as line}<span class="diff-line diff-line--{classifyDiffLine(line)}">{line}</span>{'\n'}{/each}</pre>

<style>
	.code {
		margin: 0;
		padding: var(--space-2) var(--space-3);
		background: var(--color-bg);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		overflow-x: auto;
	}

	.diff {
		white-space: pre;
	}

	.diff-line {
		display: inline;
	}
	.diff-line--add {
		color: var(--color-success);
	}
	.diff-line--del {
		color: var(--color-danger);
	}
	.diff-line--hunk {
		color: var(--color-info);
	}
	.diff-line--meta {
		color: var(--color-text-muted);
		font-weight: var(--weight-semibold);
	}
	.diff-line--context {
		color: var(--color-text-secondary);
	}
</style>
