<script lang="ts">
	// Settings pane (TD-1703): left-nav sections over the settings store.
	// The gear opens this; the wizard stays for first run only.
	//
	// Presentational — every value comes from the store, which gets it from
	// the daemon. The key section drives TD-1102's onboarding flows rather
	// than repeating them: one code path for a credential, not two.
	import {
		settings,
		closeSettings,
		setSection,
		setTheme,
		isDiscovered,
		saveSlug,
		loadRules,
		SETTINGS_SECTIONS,
		THEMES,
		type SettingsSection,
	} from '../settings.svelte.js';
	import { storeKey, validateKey, removeKey, onboarding } from '../onboarding.svelte.js';
	import { session } from '../session-status.svelte.js';
	import PolicyRuleList from './PolicyRuleList.svelte';
	import Icon from './Icon.svelte';

	const TIERS = ['brain', 'worker', 'validator'] as const;

	const SECTION_LABEL: Record<SettingsSection, string> = {
		appearance: 'Appearance',
		model: 'Model',
		policy: 'Policy',
		key: 'API key',
	};

	/** Draft slug per tier. Empty means "unchanged" — never "clear it". */
	let drafts = $state<Record<string, string>>({});
	let keyDraft = $state('');

	function slugValue(tier: string): string {
		return drafts[tier] ?? settings.tierSlugs[tier] ?? '';
	}

	function commitSlug(tier: string): void {
		const next = slugValue(tier).trim();
		if (next === '' || next === settings.tierSlugs[tier]) return;
		saveSlug(tier, next);
	}

	function pick(next: SettingsSection): void {
		setSection(next);
		// Rules are per-workspace, so they are fetched on entry rather than
		// held across workspace switches.
		if (next === 'policy') loadRules(session.sessionId);
	}

	function submitKey(): void {
		if (keyDraft.trim() === '') return;
		storeKey(keyDraft.trim());
		keyDraft = '';
	}
</script>

{#if settings.open}
	<div class="overlay" role="dialog" aria-modal="true" aria-label="Settings">
		<div class="pane">
			<nav class="nav" aria-label="Settings sections">
				{#each SETTINGS_SECTIONS as name (name)}
					<button
						class="nav-item"
						class:nav-item--active={settings.section === name}
						type="button"
						aria-current={settings.section === name ? 'page' : undefined}
						onclick={() => pick(name)}>{SECTION_LABEL[name]}</button
					>
				{/each}
			</nav>

			<div class="body">
				<div class="head">
					<h1 class="title">{SECTION_LABEL[settings.section]}</h1>
					<button class="close" type="button" aria-label="Close" onclick={closeSettings}>
						<Icon name="x" size={14} />
					</button>
				</div>

				{#if settings.section === 'appearance'}
					<div class="group" role="radiogroup" aria-label="Theme">
						{#each THEMES as option (option)}
							<button
								class="choice"
								class:choice--active={settings.theme === option}
								type="button"
								role="radio"
								aria-checked={settings.theme === option}
								onclick={() => setTheme(option)}>{option}</button
							>
						{/each}
					</div>
					<p class="hint">System follows your OS appearance and changes with it.</p>
				{:else if settings.section === 'model'}
					<p class="hint">
						Preset <strong>{settings.activePreset ?? '—'}</strong>. Edits are saved to your
						config.yaml and apply to new sessions.
					</p>
					{#each TIERS as tier (tier)}
						<label class="field">
							<span class="field-name">{tier}</span>
							<input
								class="input"
								type="text"
								value={slugValue(tier)}
								placeholder={isDiscovered(tier) ? 'discovered from the endpoint' : ''}
								disabled={settings.savingTier === tier}
								oninput={(e) => (drafts[tier] = e.currentTarget.value)}
								onblur={() => commitSlug(tier)}
							/>
						</label>
					{/each}
					{#if TIERS.some((t) => isDiscovered(t))}
						<p class="hint">
							A tier left blank asks the endpoint for its model each run. Naming one here pins it.
						</p>
					{/if}
				{:else if settings.section === 'policy'}
					<PolicyRuleList sessionId={session.sessionId} />
				{:else}
					{#if !settings.keyRequired}
						<p class="hint">
							The active preset runs on a local endpoint, so no key is sent. You can still store one
							for other presets.
						</p>
					{/if}
					<p class="hint">
						{settings.hasApiKey ? 'A key is stored in your OS keychain.' : 'No key stored.'}
					</p>
					<label class="field">
						<span class="field-name">New key</span>
						<input
							class="input"
							type="password"
							autocomplete="off"
							bind:value={keyDraft}
							placeholder="sk-…"
						/>
					</label>
					<div class="actions">
						<button class="btn" type="button" disabled={keyDraft.trim() === ''} onclick={submitKey}
							>Save key</button
						>
						<button
							class="btn"
							type="button"
							disabled={onboarding.validating}
							onclick={() => validateKey()}>Test</button
						>
						<button
							class="btn btn--danger"
							type="button"
							disabled={!settings.hasApiKey}
							onclick={removeKey}>Remove</button
						>
					</div>
					{#if onboarding.validation}
						<p class="hint" aria-live="polite">{onboarding.validation.detail}</p>
					{/if}
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.overlay {
		position: fixed;
		inset: 0;
		z-index: 40;
		display: grid;
		place-items: center;
		background: rgb(0 0 0 / 0.28);
	}

	.pane {
		display: grid;
		grid-template-columns: 10rem 1fr;
		width: min(46rem, 92vw);
		max-height: 80vh;
		background: var(--color-lifted);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-lg);
		overflow: hidden;
	}

	.nav {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: var(--space-3);
		background: var(--color-sunken);
		border-right: 1px solid var(--color-hairline);
	}

	.nav-item {
		text-align: left;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: 0;
		border-radius: var(--radius-sm);
		padding: var(--space-2);
		cursor: pointer;
	}

	.nav-item:hover {
		background: var(--color-lifted);
	}

	.nav-item--active {
		color: var(--color-ink);
		background: var(--color-lifted);
		font-weight: var(--weight-medium);
	}

	.body {
		padding: var(--space-4);
		overflow-y: auto;
	}

	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: var(--space-3);
	}

	.title {
		font-size: var(--text-md);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
		margin: 0;
	}

	.close {
		background: transparent;
		border: 0;
		color: var(--color-ink-muted);
		cursor: pointer;
		padding: var(--space-1);
		border-radius: var(--radius-sm);
	}

	.close:hover {
		color: var(--color-ink);
		background: var(--color-sunken);
	}

	.group {
		display: inline-flex;
		gap: var(--space-1);
	}

	.choice {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
		text-transform: capitalize;
	}

	.choice--active {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

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
