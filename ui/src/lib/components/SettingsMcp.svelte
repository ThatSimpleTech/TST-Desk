<script lang="ts">
	// MCP servers (TD-4403). The list is daemon truth from setup_state.
	// Command is argv tokens. A token goes to the keychain (TD-5110): the
	// value field is write-only and is cleared once it is sent.
	import {
		settings,
		saveMcpServer,
		setMcpServerEnabled,
		deleteMcpServer,
		saveMcpSecret,
		deleteMcpSecret,
		type McpServerRow,
		type McpValueRow,
	} from "../settings.svelte.js";

	let draftId = $state("");
	let draftTransport = $state<"stdio" | "http">("stdio");
	let draftCommand = $state<string[]>([""]);
	let draftUrl = $state("");
	let secretNames = $state<Record<string, string>>({});
	let secretValues = $state<Record<string, string>>({});

	function canSaveSecret(id: string): boolean {
		return (secretNames[id] ?? "").trim() !== "" && (secretValues[id] ?? "").trim() !== "";
	}

	function saveSecret(id: string): void {
		if (!canSaveSecret(id)) return;
		saveMcpSecret(id, secretNames[id] ?? "", secretValues[id] ?? "");
		secretNames[id] = "";
		secretValues[id] = "";
	}

	function valueLabel(server: McpServerRow): string {
		return server.transport === "stdio" ? "env" : "header";
	}

	function valueState(value: McpValueRow): string {
		if (value.source === "literal") return "set in config.yaml";
		return value.stored ? "in the keychain" : "not stored — save it again";
	}

	function addArg(): void {
		draftCommand = [...draftCommand, ""];
	}

	function setArg(index: number, value: string): void {
		draftCommand = draftCommand.map((token, i) => (i === index ? value : token));
	}

	function removeArg(index: number): void {
		draftCommand = draftCommand.filter((_, i) => i !== index);
		if (draftCommand.length === 0) draftCommand = [""];
	}

	function addServer(): void {
		const id = draftId.trim();
		const command = draftCommand.map((t) => t.trim()).filter((t) => t !== "");
		const url = draftUrl.trim();
		if (id === "") return;
		if (draftTransport === "stdio" && command.length === 0) return;
		if (draftTransport === "http" && url === "") return;
		const row: McpServerRow = {
			id,
			transport: draftTransport,
			command: draftTransport === "stdio" ? command : [],
			url: draftTransport === "http" ? url : "",
			enabled: true,
		};
		saveMcpServer(row);
		draftId = "";
		draftTransport = "stdio";
		draftCommand = [""];
		draftUrl = "";
	}

	function canAdd(): boolean {
		if (draftId.trim() === "") return false;
		if (draftTransport === "stdio") {
			return draftCommand.some((t) => t.trim() !== "");
		}
		return draftUrl.trim() !== "";
	}
</script>

<p class="hint">
	Listed servers load from your config.yaml. Command is argv tokens only. A server's API key or
	token goes in the keychain: name it (an env var for stdio, a header such as Authorization for
	http) and save. Only that server gets it, and the value is never shown again. Removing a server
	deletes its stored values. Changes apply to new sessions.
</p>

{#if settings.mcpServers.length === 0}
	<p class="hint">No servers listed.</p>
{/if}

{#each settings.mcpServers as server (server.id)}
	<div class="row">
		<p class="name">{server.id}</p>
		<p class="meta">
			{server.transport}
			{#if server.transport === "stdio"}
				· {server.command.join(" ") || "(no command)"}
			{:else}
				· {server.url || "(no url)"}
			{/if}
		</p>
		{#each server.excludedTools ?? [] as tool (tool.name)}
			<p class="excluded">Left out {tool.name}: {tool.reason}</p>
		{/each}
		{#each server.values ?? [] as value (value.name)}
			<div class="value">
				<p class="meta">
					{valueLabel(server)}
					{value.name} · {valueState(value)}
				</p>
				<button
					class="btn btn--danger"
					type="button"
					aria-label={`Remove ${value.name} from ${server.id}`}
					onclick={() => deleteMcpSecret(server.id, value.name)}>Remove</button
				>
			</div>
		{/each}
		<label class="field">
			<span class="field-name">{server.transport === "stdio" ? "Env var" : "Header"}</span>
			<input
				class="input"
				type="text"
				autocomplete="off"
				value={secretNames[server.id] ?? ""}
				placeholder={server.transport === "stdio" ? "GITHUB_TOKEN" : "Authorization"}
				oninput={(e) => (secretNames[server.id] = e.currentTarget.value)}
			/>
		</label>
		<label class="field">
			<span class="field-name">Value</span>
			<input
				class="input"
				type="password"
				autocomplete="off"
				value={secretValues[server.id] ?? ""}
				placeholder={server.transport === "stdio" ? "Paste token" : "Bearer …"}
				oninput={(e) => (secretValues[server.id] = e.currentTarget.value)}
			/>
		</label>
		<div class="actions">
			<button
				class="btn"
				class:btn--on={server.enabled}
				type="button"
				role="switch"
				aria-checked={server.enabled}
				onclick={() => setMcpServerEnabled(server.id, !server.enabled)}
				>{server.enabled ? "On" : "Off"}</button
			>
			<button
				class="btn"
				type="button"
				disabled={!canSaveSecret(server.id)}
				onclick={() => saveSecret(server.id)}>Save to keychain</button
			>
			<button class="btn btn--danger" type="button" onclick={() => deleteMcpServer(server.id)}
				>Remove</button
			>
		</div>
	</div>
{/each}

<p class="hint">Add a server — id is a lowercase slug.</p>
<label class="field">
	<span class="field-name">Id</span>
	<input class="input" type="text" bind:value={draftId} placeholder="example" />
</label>
<div class="field">
	<span class="field-name">Transport</span>
	<div class="group" role="radiogroup" aria-label="Transport">
		<button
			class="choice"
			class:choice--active={draftTransport === "stdio"}
			type="button"
			role="radio"
			aria-checked={draftTransport === "stdio"}
			onclick={() => (draftTransport = "stdio")}>stdio</button
		>
		<button
			class="choice"
			class:choice--active={draftTransport === "http"}
			type="button"
			role="radio"
			aria-checked={draftTransport === "http"}
			onclick={() => (draftTransport = "http")}>http</button
		>
	</div>
</div>
{#if draftTransport === "stdio"}
	{#each draftCommand as token, i (i)}
		<label class="field">
			<span class="field-name">{i === 0 ? "Command" : "Arg"}</span>
			<input
				class="input"
				type="text"
				value={token}
				placeholder={i === 0 ? "npx" : "-y"}
				oninput={(e) => setArg(i, e.currentTarget.value)}
			/>
			<button class="btn" type="button" onclick={() => removeArg(i)}>Remove</button>
		</label>
	{/each}
	<div class="actions">
		<button class="btn" type="button" onclick={addArg}>Add argument</button>
	</div>
{:else}
	<label class="field">
		<span class="field-name">URL</span>
		<input
			class="input"
			type="text"
			bind:value={draftUrl}
			placeholder="http://127.0.0.1:8765/mcp"
		/>
	</label>
{/if}
<div class="actions">
	<button class="btn" type="button" disabled={!canAdd()} onclick={addServer}>Add server</button>
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

	.name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		font-family: var(--font-mono);
		color: var(--color-ink);
		margin: 0;
	}

	.meta {
		font-size: var(--text-sm);
		font-family: var(--font-mono);
		color: var(--color-ink-secondary);
		margin: var(--space-1) 0 0;
		overflow-wrap: anywhere;
	}

	.value {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
	}

	.excluded {
		font-size: var(--text-sm);
		color: var(--color-warn);
		margin: var(--space-1) 0 0;
		overflow-wrap: anywhere;
	}

	.field {
		display: grid;
		grid-template-columns: 6rem 1fr auto;
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
	}

	.choice--active {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
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

	.btn--on {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.btn--danger:hover:not(:disabled) {
		border-color: var(--color-err);
		color: var(--color-err);
	}
</style>
