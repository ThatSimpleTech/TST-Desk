<script lang="ts">
	// Charter column on the project home (TD-4002).
	//
	// Structured §12.4 fields. Save is a human-path client message —
	// never a tool, never a steering write. YAML preview is secondary.
	import {
		charterEmptyCopy,
		charterLedeCopy,
		networkIsDeny,
		previewCharterYaml,
	} from '../charter';
	import {
		addBoundaryListItem,
		addHostItem,
		addListItem,
		charter,
		loadCharter,
		removeBoundaryListItem,
		removeHostItem,
		removeListItem,
		saveCharter,
		setBoundaryListItem,
		setCap,
		setHostItem,
		setListItem,
		setNetworkDeny,
		setNetworkHosts,
		setNotes,
		setObjective,
		startCharter,
	} from '../charter.svelte.js';

	let { workspacePath }: { workspacePath: string } = $props();

	startCharter();

	$effect(() => {
		loadCharter(workspacePath);
	});

	let hosts = $derived(
		networkIsDeny(charter.draft.boundary.network) ? [""] : charter.draft.boundary.network,
	);
	let preview = $derived(previewCharterYaml(charter.draft, charter.notes));
</script>

<section class="col" aria-label="Charter">
	<h2 class="section">Charter</h2>
	<p class="lede">{charterLedeCopy()}</p>
	{#if !charter.present}
		<p class="empty">{charterEmptyCopy()}</p>
	{/if}
	<form
		class="form"
		onsubmit={(e) => {
			e.preventDefault();
			saveCharter();
		}}
	>
		<label class="field">
			<span class="lbl">Objective</span>
			<textarea
				aria-label="Objective"
				value={charter.draft.objective}
				oninput={(e) => setObjective(e.currentTarget.value)}
			></textarea>
		</label>
		<fieldset class="field">
			<legend class="lbl">Definition of done</legend>
			{#each charter.draft.definition_of_done as item, i (i)}
				<div class="row">
					<input
						type="text"
						aria-label={`Definition of done ${i + 1}`}
						value={item}
						oninput={(e) => setListItem('definition_of_done', i, e.currentTarget.value)}
					/>
					<button class="text-btn" type="button" onclick={() => removeListItem('definition_of_done', i)}
						>Remove</button
					>
				</div>
			{/each}
			<button class="text-btn" type="button" onclick={() => addListItem('definition_of_done')}
				>Add</button
			>
		</fieldset>
		<fieldset class="field">
			<legend class="lbl">Source of truth</legend>
			{#each charter.draft.source_of_truth as item, i (i)}
				<div class="row">
					<input
						type="text"
						aria-label={`Source of truth ${i + 1}`}
						placeholder="docs/spec.md"
						value={item}
						oninput={(e) => setListItem('source_of_truth', i, e.currentTarget.value)}
					/>
					<button class="text-btn" type="button" onclick={() => removeListItem('source_of_truth', i)}
						>Remove</button
					>
				</div>
			{/each}
			<button class="text-btn" type="button" onclick={() => addListItem('source_of_truth')}
				>Add</button
			>
		</fieldset>
		<fieldset class="field">
			<legend class="lbl">Writable paths</legend>
			{#each charter.draft.boundary.writable_paths as item, i (i)}
				<div class="row">
					<input
						type="text"
						aria-label={`Writable path ${i + 1}`}
						value={item}
						oninput={(e) => setBoundaryListItem('writable_paths', i, e.currentTarget.value)}
					/>
					<button
						class="text-btn"
						type="button"
						onclick={() => removeBoundaryListItem('writable_paths', i)}>Remove</button
					>
				</div>
			{/each}
			<button class="text-btn" type="button" onclick={() => addBoundaryListItem('writable_paths')}
				>Add</button
			>
		</fieldset>
		<fieldset class="field">
			<legend class="lbl">Allowed commands</legend>
			{#each charter.draft.boundary.allowed_commands as item, i (i)}
				<div class="row">
					<input
						type="text"
						aria-label={`Allowed command ${i + 1}`}
						value={item}
						oninput={(e) => setBoundaryListItem('allowed_commands', i, e.currentTarget.value)}
					/>
					<button
						class="text-btn"
						type="button"
						onclick={() => removeBoundaryListItem('allowed_commands', i)}>Remove</button
					>
				</div>
			{/each}
			<button class="text-btn" type="button" onclick={() => addBoundaryListItem('allowed_commands')}
				>Add</button
			>
		</fieldset>
		<fieldset class="field">
			<legend class="lbl">Network</legend>
			<div class="row">
				<label class="choice">
					<input
						type="radio"
						name="charter-network"
						checked={networkIsDeny(charter.draft.boundary.network)}
						onchange={() => setNetworkDeny()}
					/>
					deny
				</label>
				<label class="choice">
					<input
						type="radio"
						name="charter-network"
						checked={!networkIsDeny(charter.draft.boundary.network)}
						onchange={() => setNetworkHosts(hosts)}
					/>
					allowlist
				</label>
			</div>
			{#if !networkIsDeny(charter.draft.boundary.network)}
				{#each hosts as host, i (i)}
					<div class="row">
						<input
							type="text"
							aria-label={`Allowed host ${i + 1}`}
							value={host}
							oninput={(e) => setHostItem(i, e.currentTarget.value)}
						/>
						<button class="text-btn" type="button" onclick={() => removeHostItem(i)}>Remove</button>
					</div>
				{/each}
				<button class="text-btn" type="button" onclick={() => addHostItem()}>Add host</button>
			{/if}
		</fieldset>
		<fieldset class="field caps">
			<legend class="lbl">Caps</legend>
			<label class="cap">
				<span class="lbl">Spend (USD)</span>
				<input
					type="number"
					min="0"
					step="0.01"
					aria-label="Spend cap in USD"
					value={charter.draft.caps.spend_usd}
					oninput={(e) => setCap('spend_usd', e.currentTarget.valueAsNumber)}
				/>
			</label>
			<label class="cap">
				<span class="lbl">Wall clock (hours)</span>
				<input
					type="number"
					min="0"
					step="0.1"
					aria-label="Wall clock hours"
					value={charter.draft.caps.wall_clock_hours}
					oninput={(e) => setCap('wall_clock_hours', e.currentTarget.valueAsNumber)}
				/>
			</label>
			<label class="cap">
				<span class="lbl">Max iterations</span>
				<input
					type="number"
					min="1"
					step="1"
					aria-label="Max iterations"
					value={charter.draft.caps.max_iterations}
					oninput={(e) => setCap('max_iterations', e.currentTarget.valueAsNumber)}
				/>
			</label>
		</fieldset>
		<fieldset class="field">
			<legend class="lbl">Stop conditions</legend>
			{#each charter.draft.stop_conditions as item, i (i)}
				<div class="row">
					<input
						type="text"
						aria-label={`Stop condition ${i + 1}`}
						value={item}
						oninput={(e) => setListItem('stop_conditions', i, e.currentTarget.value)}
					/>
					<button class="text-btn" type="button" onclick={() => removeListItem('stop_conditions', i)}
						>Remove</button
					>
				</div>
			{/each}
			<button class="text-btn" type="button" onclick={() => addListItem('stop_conditions')}
				>Add</button
			>
		</fieldset>
		<label class="field">
			<span class="lbl">Notes</span>
			<textarea
				aria-label="Charter notes"
				value={charter.notes}
				oninput={(e) => setNotes(e.currentTarget.value)}
			></textarea>
		</label>
		<details class="preview">
			<summary>YAML preview</summary>
			<pre>{preview}</pre>
		</details>
		<div class="actions">
			<button class="save" type="submit" disabled={charter.saving}>Save charter</button>
		</div>
		{#if charter.error !== null}
			<p class="err">{charter.error}</p>
		{/if}
	</form>
</section>

<style>
	.col {
		min-width: 0;
	}

	.section {
		margin: 0;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--color-ink-muted);
	}

	.lede,
	.empty {
		margin: var(--space-2) 0 0;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.empty {
		font-size: var(--text-sm);
		margin-top: var(--space-3);
	}

	.form {
		margin-top: var(--space-3);
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.field {
		margin: 0;
		padding: 0;
		border: none;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.lbl {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-ink-secondary);
	}

	.row,
	.caps {
		display: flex;
		gap: var(--space-2);
		align-items: center;
	}

	.caps {
		flex-wrap: wrap;
	}

	.cap {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		min-width: 6rem;
	}

	input,
	textarea {
		width: 100%;
		box-sizing: border-box;
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-sunken);
		color: var(--color-ink);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
	}

	textarea {
		min-height: 4rem;
		font-family: var(--font-mono);
		resize: vertical;
	}

	.choice {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.text-btn {
		border: none;
		background: transparent;
		color: var(--color-accent);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		cursor: pointer;
		padding: var(--space-1);
		flex-shrink: 0;
	}

	.save {
		border: none;
		background: var(--color-accent);
		color: var(--color-on-accent);
		border-radius: var(--radius-md);
		padding: var(--space-2) var(--space-3);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		cursor: pointer;
	}

	.save:disabled {
		background: var(--color-ink-muted);
		cursor: default;
	}

	.preview {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.preview pre {
		margin: var(--space-2) 0 0;
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-sunken);
		overflow-x: auto;
		font-family: var(--font-mono);
		color: var(--color-ink);
	}

	.err { margin: 0; font-size: var(--text-xs); color: var(--color-err); }
</style>
