<script lang="ts">
	// Chat pane (TD-1004): conversation list + cancel + composer.
	// Presentational composition only — protocol state lives in the stores.
	import { onMount } from "svelte";
	import { cancelTurn, chat, initChat, sendUserMessage, teardownChat } from "../../chat-store.svelte.js";
	import { ws } from "../../connection-status.svelte.js";
	import { canSend, showCancel } from "../../chat-store";
	import Composer from "./Composer.svelte";
	import MessageList from "./MessageList.svelte";

	onMount(() => {
		initChat();
		return teardownChat;
	});
</script>

<div class="chat-pane">
	<MessageList messages={chat.messages} />
	{#if showCancel(chat.turnState)}
		<div class="controls">
			<button
				class="cancel"
				type="button"
				onclick={() => {
					cancelTurn();
				}}
			>
				Cancel turn
			</button>
		</div>
	{/if}
	<Composer
		disabled={!canSend(chat.sessionId, ws.state)}
		onsubmit={(text) => {
			sendUserMessage(text);
		}}
	/>
</div>

<style>
	.chat-pane {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: var(--color-bg);
	}

	.controls {
		display: flex;
		justify-content: center;
		padding: var(--space-2) var(--space-4) 0;
	}

	.cancel {
		padding: var(--space-1) var(--space-4);
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-danger);
		background: var(--color-bg-raised);
		border: var(--border-width) solid var(--color-danger);
		border-radius: var(--radius-md);
		cursor: pointer;
		transition: background var(--transition-fast);
	}

	.cancel:hover {
		background: var(--color-bg-subtle);
	}
</style>
