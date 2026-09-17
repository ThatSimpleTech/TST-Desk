<script lang="ts">
	// Named API keys (TD-1717). Presence and given names only — the secret
	// never enters the settings store.
	import {
		settings,
		storeNamedKey,
		createNamedSource,
		renameCredential,
		saveCredentialHost,
		deleteNamedKey,
		validateNamedKey,
	} from "../settings.svelte.js";
	import { onboarding } from "../onboarding.svelte.js";

	let newName = $state("");
	let newKey = $state("");
	let newHost = $state("");
	let drafts = $state<Record<string, string>>({});
	let hostDrafts = $state<Record<string, string>>({});
	let keyDrafts = $state<Record<string, string>>({});

	function addSource(): void {
		if (newName.trim() === "") return;
		if (newKey.trim() !== "") {
			storeNamedKey(newName, newKey, undefined, newHost);
		} else {
			createNamedSource(newName, newHost);
		}
		newName = "";
		newKey = "";
		newHost = "";
	}

	function saveRowKey(id: string, name: string): void {
		const key = (keyDrafts[id] ?? "").trim();
		if (key === "") return;
		storeNamedKey(name, key, id);
		keyDrafts[id] = "";
	}

	function commitName(id: string): void {
		const next = (drafts[id] ?? "").trim();
		const current = settings.credentials.find((c) => c.id === id)?.name;
		if (next === "" || next === current) return;
		renameCredential(id, next);
	}

	function hostValue(id: string, stored: string | null | undefined): string {
		return hostDrafts[id] ?? stored ?? "";
	}

	function commitHost(id: string, name: string): void {
		const current = (settings.credentials.find((c) => c.id === id)?.base_url ?? "").trim();
		const next = (hostDrafts[id] ?? current).trim();
		if (next === current) return;
		saveCredentialHost(id, name, next);
	}

	/** What a stored key is for: bound tiers, plus the judgments connector. */
	function usesFor(id: string): string {
		const tiers = Object.entries(settings.tierCredentials)
			.filter(([, cred]) => cred === id)
			.map(([tier]) => tier);
		const uses = [...tiers];
		if (settings.judgmentsBackend === "typesafe" && settings.judgmentsTypesafeCredential === id) {
			uses.push("judgments");
		}
		return uses.length === 0 ? "Not in use" : `Used for: ${uses.join(", ")}`;
	}
</script>

{#if !settings.keyRequired}
	<p class="hint">
		The active preset runs on a local endpoint, so no key is sent unless a tier is bound to one
		below.
	</p>
{/if}

{#each settings.credentials as cred (cred.id)}
	<div class="row">
		<label class="field">
			<span class="field-name">Name</span>
			<input
				class="input"
				type="text"
				value={drafts[cred.id] ?? cred.name}
				oninput={(e) => (drafts[cred.id] = e.currentTarget.value)}
				onblur={() => commitName(cred.id)}
			/>
		</label>
		<label class="field">
			<span class="field-name">Host</span>
			<input
				class="input"
				type="text"
				value={hostValue(cred.id, cred.base_url)}
				placeholder="preset URL"
				oninput={(e) => (hostDrafts[cred.id] = e.currentTarget.value)}
				onblur={() => commitHost(cred.id, drafts[cred.id] ?? cred.name)}
			/>
		</label>
		{#if !cred.stored}
			<label class="field">
				<span class="field-name">Key</span>
				<input
					class="input"
					type="password"
					autocomplete="off"
					value={keyDrafts[cred.id] ?? ""}
					placeholder="sk-…"
					oninput={(e) => (keyDrafts[cred.id] = e.currentTarget.value)}
				/>
			</label>
		{/if}
		<p class="status">{cred.stored ? "Stored in the OS keychain." : "No key stored."}</p>
		<p class="uses">{usesFor(cred.id)}</p>
		<div class="actions">
			{#if !cred.stored}
				<button
					class="btn"
					type="button"
					disabled={(keyDrafts[cred.id] ?? "").trim() === ""}
					onclick={() => saveRowKey(cred.id, drafts[cred.id] ?? cred.name)}>Save key</button
				>
			{/if}
			<button
				class="btn"
				type="button"
				disabled={onboarding.validating || !cred.stored}
				onclick={() => validateNamedKey(cred.id)}>Test</button
			>
			<button class="btn btn--danger" type="button" onclick={() => deleteNamedKey(cred.id)}
				>Remove</button
			>
		</div>
	</div>
{/each}

<div class="add">
<p class="hint">
	Add a source — the name is what Model settings will show. Host is the OpenAI-compatible
	<code>/v1</code> URL this key talks to; leave it blank to keep the preset's endpoint. Key can
	wait.
</p>
<label class="field">
	<span class="field-name">Name</span>
	<input class="input" type="text" bind:value={newName} placeholder="OpenRouter, EZER, …" />
</label>
<label class="field">
	<span class="field-name">Key</span>
	<input
		class="input"
		type="password"
		autocomplete="off"
		bind:value={newKey}
		placeholder="sk-…"
	/>
</label>
<label class="field">
	<span class="field-name">Host</span>
	<input
		class="input"
		type="text"
		bind:value={newHost}
		placeholder="http://127.0.0.1:8000/v1"
	/>
</label>
<div class="actions">
	<button
		class="btn"
		type="button"
		disabled={newName.trim() === ""}
		onclick={addSource}>Add source</button
	>
</div>
{#if onboarding.validation}
	<p class="hint" aria-live="polite">{onboarding.validation.detail}</p>
{/if}
</div>

<style>
	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-3) 0 0;
	}

	.row {
		margin-top: var(--space-4);
		padding-top: var(--space-3);
		border-top: 1px solid var(--color-hairline);
	}

	.row:first-of-type {
		border-top: 0;
		padding-top: 0;
	}

	.add {
		position: sticky;
		bottom: 0;
		z-index: 1;
		margin-top: var(--space-4);
		padding-top: var(--space-3);
		padding-bottom: var(--space-1);
		background: var(--color-lifted);
		border-top: 1px solid var(--color-hairline);
	}

	.status {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-2) 0 0;
	}

	.uses {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		margin: var(--space-1) 0 0;
	}

	.field {
		display: grid;
		grid-template-columns: 6rem 1fr;
		align-items: center;
		gap: var(--space-3);
		margin-top: var(--space-3);
	}

	.field-name {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.input {
		font-family: var(--font-mono);
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-ground);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-2);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
		margin-top: var(--space-3);
	}

	.btn {
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.btn:hover:not(:disabled) {
		border-color: var(--color-accent);
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.btn--danger:hover:not(:disabled) {
		border-color: var(--color-err);
		color: var(--color-err);
	}
</style>
