<script lang="ts">
	// Background delegate workers (TD-5299). The daemon owns them; this lists
	// its newest `worker_jobs`, offers Stop on a live one, and shows a settled
	// one's steps from `worker_result`.
	import { formatWorkerTranscript } from '../worker-transcript';
	import {
		isLive,
		resultFor,
		stopWorker,
		workerStateLabel,
		workersFor,
	} from '../workers-store.svelte.js';

	interface Props {
		sessionId: string;
	}

	let { sessionId }: Props = $props();

	let list = $derived(workersFor(sessionId));
	let live = $derived(list.filter(isLive).length);
</script>

{#if list.length > 0}
	<section class="workers" aria-label="Background workers">
		<p class="progress">Background workers · {live} running</p>
		<ul class="rows">
			{#each list as worker (worker.worker_id)}
				{@const result = resultFor(sessionId, worker.worker_id)}
				<li class="row">
					<div class="line">
						<span class="state">{workerStateLabel(worker)}</span>
						<span class="label" title={worker.task}>{worker.label}</span>
						<span class="meta">{worker.calls} calls · ${worker.cost_usd.toFixed(2)}</span>
						{#if isLive(worker)}
							<button
								class="stop"
								type="button"
								onclick={() => stopWorker(sessionId, worker.worker_id)}>Stop</button
							>
						{/if}
					</div>
					{#if isLive(worker) && worker.last_step}
						<p class="step">{worker.last_step}</p>
					{/if}
					{#if worker.branch}
						<p class="step">Branch {worker.branch}</p>
					{/if}
					{#if result !== undefined && result.transcript.length > 0}
						<details>
							<summary>Steps</summary>
							<pre class="transcript">{formatWorkerTranscript(result.transcript)}</pre>
						</details>
					{/if}
				</li>
			{/each}
		</ul>
	</section>
{/if}

<style>
	.workers {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}
	.progress {
		margin: 0;
		color: var(--color-ink-muted);
		font-size: var(--text-sm);
	}
	.rows {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		font-size: var(--text-sm);
	}
	.line {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		min-width: 0;
	}
	.state {
		color: var(--color-ink-secondary);
		flex-shrink: 0;
	}
	.label {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.meta {
		color: var(--color-ink-muted);
		flex-shrink: 0;
	}
	.stop {
		font-size: var(--text-xs);
		padding: var(--space-1) var(--space-2);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		background: var(--color-ground);
		color: var(--color-ink);
		cursor: pointer;
	}
	.step {
		margin: 0;
		color: var(--color-ink-muted);
		font-size: var(--text-xs);
		overflow-wrap: anywhere;
	}
	.transcript {
		font-size: var(--text-xs);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		margin: var(--space-1) 0 0;
	}
</style>
