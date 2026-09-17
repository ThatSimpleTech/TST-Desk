<script lang="ts">
	// Judgment-seam toggles (TD-708/709/710/711, dev build). Every feature
	// defaults off and fails closed; the backend is the worker tier the
	// user already runs — no external judgments API is required.
	import { settings, setJudgments } from '../settings.svelte.js';
</script>

<div class="block">
	<div class="head">
		<p class="title">Judgments</p>
		<span class="badge">Dev</span>
	</div>
	<p class="hint">
		Small typed decisions on the worker model you already run — no additional service; they
		use your configured worker provider. Every judgment fails closed: an unavailable or
		unsure answer is the same as no judgment. Applies to new sessions.
	</p>
</div>

<div class="row">
	<div>
		<p class="title">Verify actions</p>
		<p class="hint">
			After a computer-use action, check it had its intended effect from a compact text
			before/after state — never a screenshot. A refuted action is flagged so the agent can
			re-check or retry. On drivers that cannot describe their state, the judgment reports
			unavailable and the action proceeds unverified.
		</p>
	</div>
	<button
		class="choice"
		class:choice--active={settings.judgmentsVerification}
		type="button"
		role="switch"
		aria-checked={settings.judgmentsVerification}
		onclick={() => setJudgments({ verification: !settings.judgmentsVerification })}
		>{settings.judgmentsVerification ? 'On' : 'Off'}</button
	>
</div>

<div class="row">
	<div>
		<p class="title">Stop spinning unattended runs</p>
		<p class="hint">
			A run that keeps acting without approaching its objective trips a circuit breaker and
			reports, instead of burning spend until the cap.
		</p>
	</div>
	<button
		class="choice"
		class:choice--active={settings.judgmentsSemanticBreaker}
		type="button"
		role="switch"
		aria-checked={settings.judgmentsSemanticBreaker}
		onclick={() => setJudgments({ semanticBreaker: !settings.judgmentsSemanticBreaker })}
		>{settings.judgmentsSemanticBreaker ? 'On' : 'Off'}</button
	>
</div>

<div class="row">
	<div>
		<p class="title">Pick browser elements by judgment</p>
		<p class="hint">
			Adds a browser_pick tool: code extracts candidate elements as a fixed schema
			(index, role, name, box) and the judgment picks one — no page text, HTML, cookies,
			or URL crosses the boundary. No confident match falls back to coordinate clicking.
		</p>
	</div>
	<button
		class="choice"
		class:choice--active={settings.judgmentsCandidateSelection}
		type="button"
		role="switch"
		aria-checked={settings.judgmentsCandidateSelection}
		onclick={() => setJudgments({ candidateSelection: !settings.judgmentsCandidateSelection })}
		>{settings.judgmentsCandidateSelection ? 'On' : 'Off'}</button
	>
</div>

<div class="row">
	<div>
		<p class="title">Confidence threshold</p>
		<p class="hint">
			Below this, a judgment counts as unavailable and nothing changes hands. 0.6 is the
			default.
		</p>
	</div>
	<input
		class="field number"
		type="number"
		min="0"
		max="1"
		step="0.05"
		value={settings.judgmentsConfidenceThreshold}
		aria-label="Confidence threshold"
		onchange={(event) => {
			const value = Number(event.currentTarget.value);
			if (Number.isFinite(value) && value >= 0 && value <= 1) {
				setJudgments({ confidenceThreshold: value });
			}
		}}
	/>
</div>

<style>
	.block {
		margin-bottom: var(--space-4);
	}

	.head {
		display: flex;
		align-items: center;
		gap: var(--space-2);
	}

	.badge {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: 0 var(--space-2);
	}

	.row {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-3);
		margin-top: var(--space-4);
		padding-top: var(--space-4);
		border-top: 1px solid var(--color-hairline);
	}

	.title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		margin: 0;
	}

	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-1) 0 0;
	}

	.choice {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
		flex-shrink: 0;
	}

	.choice--active {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.choice:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.field {
		width: 5.5rem;
		flex-shrink: 0;
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-sunken);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
	}
</style>
