<script lang="ts">
	// SMTP settings (TD-3820). The password is typed here and sent once.
	// The daemon stores it in the keychain and never echoes it back.
	import { settings, saveEmailNotify, sendTestEmail } from '../settings.svelte.js';

	let enabled = $state(false);
	let host = $state('');
	let port = $state('587');
	let security = $state<'starttls' | 'tls'>('starttls');
	let username = $state('');
	let fromAddress = $state('');
	let password = $state('');
	let testTo = $state('');

	// Copy the daemon's form. The password field stays local: a later
	// setup_state must not put a secret back into the input.
	$effect(() => {
		enabled = settings.emailEnabled;
		host = settings.emailHost;
		port = String(settings.emailPort);
		security = settings.emailSecurity;
		username = settings.emailUsername;
		fromAddress = settings.emailFrom;
	});

	function save(): void {
		const parsed = Number(port);
		saveEmailNotify({
			enabled,
			host: host.trim(),
			port: Number.isInteger(parsed) && parsed >= 1 ? parsed : 587,
			security,
			username: username.trim(),
			from_address: fromAddress.trim(),
			password: password === '' ? null : password,
		});
		password = '';
	}

	function sendTest(): void {
		sendTestEmail(testTo.trim());
	}
</script>

<p class="hint">
	Scheduled jobs can mail their report. The password is stored in the OS keychain, not in
	config.yaml.
	{#if settings.emailPasswordStored}
		A password is already stored. Leave the field blank to keep it.
	{/if}
</p>

<div class="keep">
	<p class="keep-title">Enabled</p>
	<button
		class="choice"
		class:choice--active={enabled}
		type="button"
		role="switch"
		aria-checked={enabled}
		onclick={() => (enabled = !enabled)}>{enabled ? 'On' : 'Off'}</button
	>
</div>

<label class="field">
	<span class="field-name">Host</span>
	<input class="input" type="text" bind:value={host} placeholder="smtp.example.com" />
</label>
<label class="field">
	<span class="field-name">Port</span>
	<input class="input" type="text" bind:value={port} />
</label>
<label class="field">
	<span class="field-name">Security</span>
	<select class="input" bind:value={security}>
		<option value="starttls">STARTTLS</option>
		<option value="tls">TLS</option>
	</select>
</label>
<label class="field">
	<span class="field-name">Username</span>
	<input class="input" type="text" bind:value={username} />
</label>
<label class="field">
	<span class="field-name">From</span>
	<input class="input" type="text" bind:value={fromAddress} placeholder="desk@example.com" />
</label>
<label class="field">
	<span class="field-name">Password</span>
	<input
		class="input"
		type="password"
		bind:value={password}
		autocomplete="new-password"
		placeholder={settings.emailPasswordStored ? 'Stored in the keychain' : 'Keychain only'}
	/>
</label>

<div class="actions">
	<button class="btn" type="button" onclick={save}>Save</button>
</div>

<label class="field">
	<span class="field-name">Test to</span>
	<input class="input" type="email" bind:value={testTo} placeholder="owner@example.com" />
</label>
<div class="actions">
	<button class="btn" type="button" onclick={sendTest}>Send test email</button>
</div>
{#if settings.emailTestOk === true}
	<p class="ok">{settings.emailTestMessage}</p>
{:else if settings.emailTestOk === false}
	<p class="error" role="alert">{settings.emailTestErrorClass}: {settings.emailTestMessage}</p>
{/if}

<style>
	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: 0 0 var(--space-3);
	}

	.keep {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		margin-top: var(--space-3);
	}

	.keep-title {
		font-size: var(--text-sm);
		color: var(--color-ink);
		margin: 0;
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

	.choice {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.choice--active {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.actions {
		margin-top: var(--space-3);
	}

	.btn {
		font-size: var(--text-sm);
		color: var(--color-on-accent);
		background: var(--color-accent);
		border: 0;
		border-radius: var(--radius-sm);
		padding: var(--space-2) var(--space-3);
		cursor: pointer;
	}

	.ok {
		font-size: var(--text-sm);
		color: var(--color-ok);
		margin: var(--space-3) 0 0;
	}

	.error {
		font-size: var(--text-sm);
		color: var(--color-err);
		margin: var(--space-3) 0 0;
	}
</style>
