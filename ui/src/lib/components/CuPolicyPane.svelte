<script lang="ts">
	// Computer-use policy (TD-4830). Mirrors Claude Desktop's Computer use
	// panel: master switch, background vs full control, unhide-on-finish,
	// denied apps, and the two macOS grants.
	import { settings, setCuPolicy } from '../settings.svelte.js';
	import { cuPermissions } from '../cu-permissions.svelte.js';
	import CuPermissionsPane from './CuPermissionsPane.svelte';

	let draft = $state('');

	function addDenied(): void {
		const name = draft.trim();
		if (!name) return;
		if (settings.cuDeniedApps.some((item) => item.toLowerCase() === name.toLowerCase())) {
			draft = '';
			return;
		}
		setCuPolicy({ deniedApps: [...settings.cuDeniedApps, name] });
		draft = '';
	}

	function removeDenied(name: string): void {
		setCuPolicy({ deniedApps: settings.cuDeniedApps.filter((item) => item !== name) });
	}

	function grantLabel(ok: boolean, stale: boolean): string {
		if (ok) return 'Granted';
		if (stale) return 'Stale';
		return 'Not granted';
	}
</script>

<div class="block">
	<div class="head">
		<p class="title">Computer use</p>
		<span class="badge">Beta</span>
	</div>
	<p class="hint">Not available in cloud sessions, which don't run on this computer.</p>
</div>

<div class="row">
	<div>
		<p class="title">Enable computer use</p>
		<p class="hint">
			Let the agent see and act in the apps you allow, working in the background or with full
			control of your screen.
		</p>
	</div>
	<button
		class="choice"
		class:choice--active={settings.cuEnabled}
		type="button"
		role="switch"
		aria-checked={settings.cuEnabled}
		onclick={() => setCuPolicy({ enabled: !settings.cuEnabled })}
		>{settings.cuEnabled ? 'On' : 'Off'}</button
	>
</div>

<div class="row">
	<div>
		<p class="title">Unhide apps when the agent finishes</p>
		<p class="hint">
			When the agent has full control of your screen, apps hidden during a task are restored
			when it stops.
		</p>
	</div>
	<button
		class="choice"
		class:choice--active={settings.cuUnhideOnFinish}
		type="button"
		role="switch"
		aria-checked={settings.cuUnhideOnFinish}
		onclick={() => setCuPolicy({ unhideOnFinish: !settings.cuUnhideOnFinish })}
		>{settings.cuUnhideOnFinish ? 'On' : 'Off'}</button
	>
</div>

<div class="mode">
	<div>
		<p class="title">How the agent uses the apps you allow</p>
		<p class="hint">
			Background: works inside allowed apps while you keep using your computer, and asks
			before taking the pointer. Full control: takes the screen, mouse, and keyboard by
			default whenever it uses an allowed app.
		</p>
	</div>
	<div class="seg" role="radiogroup" aria-label="Computer-use mode">
		<button
			class="choice"
			class:choice--active={settings.cuMode === 'background'}
			type="button"
			role="radio"
			aria-checked={settings.cuMode === 'background'}
			onclick={() => setCuPolicy({ mode: 'background' })}>Background</button
		>
		<button
			class="choice"
			class:choice--active={settings.cuMode === 'full_control'}
			type="button"
			role="radio"
			aria-checked={settings.cuMode === 'full_control'}
			onclick={() => setCuPolicy({ mode: 'full_control' })}>Full control</button
		>
	</div>
</div>

<div class="denied">
	<p class="title">Denied apps</p>
	<p class="hint">
		Any request the agent makes to access these apps is automatically rejected. It may still
		affect them indirectly through actions in allowed apps.
	</p>
	{#if settings.cuDeniedApps.length === 0}
		<p class="empty">No apps denied. Add an app to automatically reject requests for it.</p>
	{:else}
		<ul class="list">
			{#each settings.cuDeniedApps as name (name)}
				<li>
					<span>{name}</span>
					<button class="link" type="button" onclick={() => removeDenied(name)}>Remove</button>
				</li>
			{/each}
		</ul>
	{/if}
	<form
		class="add"
		onsubmit={(event) => {
			event.preventDefault();
			addDenied();
		}}
	>
		<input
			class="field"
			type="text"
			placeholder="App name or bundle id"
			bind:value={draft}
			aria-label="Denied app name"
		/>
		<button class="btn" type="submit">Add app</button>
	</form>
</div>

<div class="grant">
	<p class="title">Accessibility</p>
	<span
		class="status"
		class:status--ok={cuPermissions.accessibility}
		class:status--stale={!cuPermissions.accessibility && cuPermissions.staleAccessibility}
		>{grantLabel(cuPermissions.accessibility, cuPermissions.staleAccessibility)}</span
	>
</div>
<div class="grant">
	<p class="title">Screen recording</p>
	<span
		class="status"
		class:status--ok={cuPermissions.screenRecording}
		class:status--stale={!cuPermissions.screenRecording && cuPermissions.staleScreenRecording}
		>{grantLabel(cuPermissions.screenRecording, cuPermissions.staleScreenRecording)}</span
	>
</div>
<CuPermissionsPane variant="settings" />

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

	.row,
	.mode,
	.denied,
	.grant {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-3);
		margin-top: var(--space-4);
		padding-top: var(--space-4);
		border-top: 1px solid var(--color-hairline);
	}

	.denied,
	.mode {
		flex-direction: column;
	}

	.grant {
		align-items: center;
	}

	.title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		margin: 0;
	}

	.hint,
	.empty {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-1) 0 0;
	}

	.seg {
		display: flex;
		gap: var(--space-2);
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

	.list {
		list-style: none;
		margin: var(--space-2) 0 0;
		padding: 0;
		width: 100%;
	}

	.list li {
		display: flex;
		align-items: center;
		justify-content: space-between;
		font-size: var(--text-sm);
		padding: var(--space-1) 0;
	}

	.add {
		display: flex;
		gap: var(--space-2);
		width: 100%;
		margin-top: var(--space-2);
	}

	.field {
		flex: 1;
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-sunken);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
	}

	.btn,
	.link {
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.link {
		border: 0;
		color: var(--color-ink-secondary);
	}

	.status {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
	}

	.status--ok {
		color: var(--color-ok);
	}

	.status--stale {
		color: var(--color-warn, var(--color-err));
	}
</style>
