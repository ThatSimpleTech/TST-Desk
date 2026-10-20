<script lang="ts">
	// The comments already sent on one file, each open until a later write
	// touches its lines, then resolved (TD-5132). The status comes from the
	// daemon's own events: the turn that carried the comment, and the diffs
	// of the writes after it.
	import { spanLabel, type SentComment } from '../review-comments';

	let { comments }: { comments: SentComment[] } = $props();
</script>

<ul class="sent" aria-label="Sent comments">
	{#each comments as c (c.id)}
		<li class="comment" class:comment--resolved={c.status === 'resolved'}>
			<span class="status">{c.status === 'resolved' ? 'Resolved' : 'Open'}</span>
			<span class="span">{spanLabel(c.start_line, c.end_line)}</span>
			<span class="body">{c.body}</span>
		</li>
	{/each}
</ul>

<style>
	.sent {
		list-style: none;
		margin: var(--space-2) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		font-size: var(--text-xs);
	}

	.comment {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		color: var(--color-ink);
	}

	.status {
		flex-shrink: 0;
		padding: 0 var(--space-1);
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--color-accent) 14%, transparent);
		color: var(--color-accent);
		font-weight: var(--weight-semibold);
	}

	.comment--resolved .status {
		background: color-mix(in srgb, var(--color-ok) 14%, transparent);
		color: var(--color-ok);
	}

	.comment--resolved .body {
		color: var(--color-ink-muted);
		text-decoration: line-through;
	}

	.span {
		flex-shrink: 0;
		font-family: var(--font-mono);
		color: var(--color-ink-muted);
	}

	.body {
		flex: 1;
		min-width: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
</style>
