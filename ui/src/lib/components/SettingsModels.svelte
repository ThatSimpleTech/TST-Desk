<script lang="ts">
	import {
		settings,
		isDiscovered,
		saveSlug,
		saveTierCredential,
		selectedCredential,
		credentialHost,
		closeSettings,
		refreshModelCards,
		saveContextWindow,
	} from "../settings.svelte.js";
	import { cardLine, cardNote } from "../model-card";
	import { modelLabel } from "../effort";
	import { MODELS_DOC_URL, openDoc } from "../docs-links";
	import { openAddModel } from "../onboarding.svelte.js";
	import { onMount } from "svelte";
	import { sendToDaemon } from "../connection-status.svelte.js";

	// TD-5197: the daemon refreshes model cards in the background, so the
	// numbers may have changed since the window last asked. Read them fresh
	// each time this page opens.
	onMount(() => sendToDaemon({ type: "get_setup_state" }));

	// TD-5172: the model section leads with the active model and the "Add a
	// model" walkthrough. The per-role editor below it is framed as "Build a
	// team": most people run one model in all three roles and never open it.
	const TIERS = ["brain", "worker", "validator"] as const;

	// Both are modal: close Settings so the walkthrough is on top.
	function addModel(): void {
		closeSettings();
		openAddModel();
	}

	const ROLE: Record<(typeof TIERS)[number], { name: string; job: string }> = {
		brain: { name: "Brain", job: "Plans the work and talks with you." },
		worker: { name: "Worker", job: "Does the edits, searches and commands." },
		validator: { name: "Validator", job: "Checks the work before a turn ends." },
	};

	let drafts = $state<Record<string, string>>({});

	function slugValue(tier: string): string {
		return drafts[tier] ?? settings.tierSlugs[tier] ?? "";
	}

	function commitSlug(tier: string): void {
		const next = slugValue(tier).trim();
		if (next === "" || next === settings.tierSlugs[tier]) return;
		saveSlug(tier, next);
	}

	// TD-5189: the context window, editable. Saved on blur or Enter.
	let windowDrafts = $state<Record<string, string>>({});

	function windowValue(tier: string): string {
		return windowDrafts[tier] ?? String(settings.tierCards[tier]?.context_window ?? "");
	}

	function commitWindow(tier: string): void {
		const raw = windowDrafts[tier];
		delete windowDrafts[tier];
		if (raw === undefined) return;
		const next = Number(raw.replace(/[\s,_]/g, ""));
		saveContextWindow(tier, next);
	}

	function credentialLabel(id: string): string {
		return settings.credentials.find((c) => c.id === id)?.name ?? id;
	}
</script>

{#if !settings.hasModel || !settings.activePreset}
	<p class="hint">No model is set up yet. Add one to start chatting.</p>
{:else}
	<p class="hint">
		Active model <strong>{modelLabel(settings.activePreset, settings.presetLabels)}</strong>. New
		chats use it; the model picker under the message box switches any chat.
	</p>
{/if}
<div class="actions">
	<button class="btn" type="button" onclick={addModel}>Add a model</button>
	<button class="btn btn--ghost" type="button" onclick={() => openDoc(MODELS_DOC_URL)}>
		How models work
	</button>
</div>

{#if settings.hasModel && settings.activePreset}
	<h3 class="heading">Build a team</h3>
	<p class="hint">
		Every turn has three roles. One model can fill all of them, and that is the normal setup. A
		team gives each role its own model: worth it when a strong, costly model should plan while a
		cheaper or local one does the bulk of the work. Edits here change the active model's entry in
		your config.yaml and apply to new sessions.
	</p>
	{#each TIERS as tier (tier)}
		<label class="field">
			<span class="field-name">{ROLE[tier].name}</span>
			<input
				class="input"
				type="text"
				aria-label={`${ROLE[tier].name} model`}
				value={slugValue(tier)}
				placeholder={isDiscovered(tier) ? "discovered from the endpoint" : ""}
				disabled={settings.savingTier === tier}
				oninput={(e) => (drafts[tier] = e.currentTarget.value)}
				onblur={() => commitSlug(tier)}
			/>
		</label>
		<p class="hint role">{ROLE[tier].job}</p>
		<label class="field">
			<span class="field-name">Key</span>
			<select
				class="input"
				value={selectedCredential(tier)}
				disabled={settings.savingCredentialTier === tier}
				onchange={(e) => saveTierCredential(tier, e.currentTarget.value)}
			>
				{#if settings.tierLoopback[tier]}
					<option value="">None (no key)</option>
				{/if}
				{#each settings.credentials as cred (cred.id)}
					<option value={cred.id}>{cred.name}</option>
				{/each}
				{#if !settings.credentials.some((c) => c.id === selectedCredential(tier)) && selectedCredential(tier) !== ""}
					<option value={selectedCredential(tier)}
						>{credentialLabel(selectedCredential(tier))}</option
					>
				{/if}
			</select>
		</label>
		{#if credentialHost(selectedCredential(tier))}
			<p class="hint host">{credentialHost(selectedCredential(tier))}</p>
		{/if}
		{#if settings.tierCards[tier]}
			<label class="field">
				<span class="field-name">Context</span>
				<input
					class="input"
					type="text"
					inputmode="numeric"
					aria-label={`${ROLE[tier].name} context window`}
					value={windowValue(tier)}
					disabled={settings.savingWindowTier === tier}
					oninput={(e) => (windowDrafts[tier] = e.currentTarget.value)}
					onblur={() => commitWindow(tier)}
					onkeydown={(e) => {
						if (e.key === "Enter") e.currentTarget.blur();
					}}
				/>
			</label>
			<p class="hint role">
				Tokens this role can hold before older turns are summarised. Refresh reads it from the
				server when the server says what it serves.
			</p>
			<p class="hint role card">{cardLine(settings.tierCards[tier])}</p>
			{#if cardNote(settings.tierCards[tier])}
				<p class="hint role">{cardNote(settings.tierCards[tier])}</p>
			{/if}
		{/if}
	{/each}
	<div class="actions">
		<button
			class="btn"
			type="button"
			disabled={settings.refreshingCards}
			onclick={refreshModelCards}
		>
			{settings.refreshingCards ? "Refreshing…" : "Refresh from servers"}
		</button>
	</div>
	<p class="hint">
		Asks each role's server for its model card and saves the prices, context size and output
		limit it lists. A server on this computer only sets the context size when it says what it
		serves. New chats use them; open chats keep theirs.
	</p>
	{#if TIERS.some((t) => isDiscovered(t))}
		<p class="hint">
			A role left blank asks the endpoint for its model each run. Naming one here pins it.
		</p>
	{/if}
	<p class="hint">
		To list a team on its own in the picker, give its entry <code>picker: team</code> in config.yaml.
		The guide has an example.
	</p>
{/if}

<style>
	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-3) 0 0;
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

	.host {
		margin-top: var(--space-1);
		font-family: var(--font-mono);
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

	.input:disabled {
		opacity: 0.6;
	}

	.card {
		font-family: var(--font-mono);
	}

	.btn:disabled {
		opacity: 0.6;
		cursor: default;
	}

	.heading {
		margin: var(--space-5) 0 0;
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
	}

	.role {
		margin: var(--space-1) 0 0 calc(6rem + var(--space-3));
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
		margin-top: var(--space-3);
	}

	.btn {
		font-size: var(--text-sm);
		padding: var(--space-1) var(--space-3);
		border-radius: var(--radius-sm);
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-ground);
		color: var(--color-ink);
		cursor: pointer;
	}

	.btn--ghost {
		border-color: transparent;
		background: transparent;
		color: var(--color-ink-secondary);
	}
</style>
