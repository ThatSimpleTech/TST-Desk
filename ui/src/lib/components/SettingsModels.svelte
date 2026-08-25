<script lang="ts">
	import {
		settings,
		isDiscovered,
		saveSlug,
		saveTierCredential,
		selectedCredential,
		credentialHost,
	} from "../settings.svelte.js";

	const TIERS = ["brain", "worker", "validator"] as const;

	let drafts = $state<Record<string, string>>({});

	function slugValue(tier: string): string {
		return drafts[tier] ?? settings.tierSlugs[tier] ?? "";
	}

	function commitSlug(tier: string): void {
		const next = slugValue(tier).trim();
		if (next === "" || next === settings.tierSlugs[tier]) return;
		saveSlug(tier, next);
	}

	function credentialLabel(id: string): string {
		return settings.credentials.find((c) => c.id === id)?.name ?? id;
	}
</script>

<p class="hint">
	Preset <strong>{settings.activePreset ?? "—"}</strong>. Edits are saved to your config.yaml and
	apply to new sessions.
</p>
{#each TIERS as tier (tier)}
	<label class="field">
		<span class="field-name">{tier}</span>
		<input
			class="input"
			type="text"
			value={slugValue(tier)}
			placeholder={isDiscovered(tier) ? "discovered from the endpoint" : ""}
			disabled={settings.savingTier === tier}
			oninput={(e) => (drafts[tier] = e.currentTarget.value)}
			onblur={() => commitSlug(tier)}
		/>
	</label>
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
{/each}
{#if TIERS.some((t) => isDiscovered(t))}
	<p class="hint">
		A tier left blank asks the endpoint for its model each run. Naming one here pins it.
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
</style>
