<script lang="ts">
	// "Paste JSON" on the MCP page (TD-5253): the block from a server's
	// README becomes server entries, with tokens in the keychain. Nothing is
	// saved or started until Import is pressed, and the exact command each
	// server would run is on screen first.
	import { onMount } from 'svelte';
	import { settings } from '../settings.svelte.js';
	import {
		canImport,
		isStale,
		mcpImport,
		previewImport,
		rowProblem,
		runImport,
		startMcpImport,
		takenIds,
	} from '../mcp-import.svelte.js';

	onMount(startMcpImport);

	let open = $state(false);
	let listed = $derived(settings.mcpServers.map((server) => server.id));
	let stale = $derived(isStale());
</script>

<div class="import">
	<button class="btn" type="button" aria-expanded={open} onclick={() => (open = !open)}
		>Paste JSON</button
	>
	{#if mcpImport.done}
		<p class="hint" role="status">{mcpImport.done}</p>
	{/if}
	{#if open}
		<p class="hint">
			Paste the block from a server's README (an mcpServers list). You will see what each
			server would run before anything is saved. Tokens go to the keychain, not to config.yaml.
		</p>
		<textarea
			class="text"
			rows="6"
			spellcheck="false"
			autocomplete="off"
			aria-label="MCP server JSON"
			bind:value={mcpImport.text}
		></textarea>
		<div class="actions">
			<button
				class="btn"
				type="button"
				disabled={mcpImport.busy || mcpImport.text.trim() === ''}
				onclick={previewImport}>Preview</button
			>
		</div>
		{#if mcpImport.error}
			<p class="error" role="alert">{mcpImport.error}</p>
		{/if}
		{#if stale}
			<p class="hint">The text changed. Preview it again before importing.</p>
		{/if}
		{#each mcpImport.rows as row (row.server.source_name)}
			{@const problem = rowProblem(row, takenIds(row, mcpImport.rows, listed))}
			<div class="server" class:server--off={!row.chosen}>
				<label class="pick">
					<input
						type="checkbox"
						bind:checked={row.chosen}
						disabled={row.server.refused !== ''}
						aria-label={`Import ${row.server.source_name}`}
					/>
					<span class="name">{row.server.source_name}</span>
				</label>
				{#if row.server.refused}
					<p class="error">Cannot be imported: {row.server.refused}.</p>
				{:else}
					<label class="field">
						<span class="field-name">Id</span>
						<input class="input" type="text" bind:value={row.id} aria-label={`Id for ${row.server.source_name}`} />
					</label>
					<p class="runs">
						{#if row.server.transport === 'stdio'}
							Runs: <code>{row.server.command.join(' ')}</code>
						{:else}
							Connects to: <code>{row.server.url}</code>
						{/if}
					</p>
					{#each row.server.notes as note (note)}
						<p class="note">{note}</p>
					{/each}
					{#if row.server.off_box}
						<label class="pick">
							<input
								type="checkbox"
								bind:checked={row.allowRemote}
								aria-label={`Allow connecting to ${row.server.url}`}
							/>
							<span>Allow connecting to this server on another machine</span>
						</label>
					{/if}
					{#each row.server.values as value (value.name)}
						{#if value.disposition === 'needs_input'}
							<label class="field">
								<span class="field-name">{value.name}</span>
								<input
									class="input"
									type="password"
									autocomplete="off"
									aria-label={`Value for ${value.name}`}
									bind:value={row.inputs[value.name]}
								/>
								<span class="note">{value.reason} It goes to the keychain.</span>
							</label>
						{:else}
							<p class="value">
								{value.name} ·
								{value.disposition === 'keychain' ? 'goes to the keychain' : 'written to config.yaml'}
							</p>
						{/if}
					{/each}
					{#if row.server.not_imported.length > 0}
						<p class="note">Not imported: {row.server.not_imported.join(', ')}.</p>
					{/if}
					{#if row.chosen && problem}
						<p class="error">{problem}</p>
					{/if}
				{/if}
			</div>
		{/each}
		{#if mcpImport.rows.length > 0}
			<div class="actions">
				<button
					class="btn btn--on"
					type="button"
					disabled={mcpImport.busy || !canImport(mcpImport.rows, listed, stale)}
					onclick={() => runImport(listed)}>Import</button
				>
			</div>
		{/if}
	{/if}
</div>

<style>
	.import {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		margin-top: var(--space-4);
		align-items: flex-start;
	}

	.hint,
	.note,
	.value,
	.runs {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.note {
		font-size: var(--text-xs);
	}

	.runs code {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink);
		overflow-wrap: anywhere;
	}

	.error {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-err);
	}

	.text {
		width: 100%;
		box-sizing: border-box;
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink);
		background: var(--color-lifted);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: var(--space-2);
	}

	.server {
		width: 100%;
		box-sizing: border-box;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		padding: var(--space-2) var(--space-3);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
	}

	.server--off {
		opacity: 0.7;
	}

	.pick {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.name {
		font-weight: var(--weight-medium);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.field-name {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.input {
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-lifted);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-2);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
	}

	.btn {
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.btn--on {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-on-accent);
	}
</style>
