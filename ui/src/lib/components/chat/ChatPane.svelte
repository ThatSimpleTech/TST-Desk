<script lang="ts">
	// Chat pane (TD-1004, restyled TD-1604): conversation + composer held in
	// one centered column (max ≈760px) on wide windows. Cancel lives in the
	// composer's send→stop morph; Esc cancels too (TD-1609). Presentational
	// composition only — protocol state lives in the stores.
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
	<div class="column">
		<MessageList messages={chat.messages} />
		<Composer
			disabled={!canSend(chat.sessionId, ws.state)}
			running={showCancel(chat.turnState)}
			onsubmit={(text) => {
				sendUserMessage(text);
			}}
			oncancel={cancelTurn}
		/>
	</div>
</div>

<style>
	.chat-pane {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: var(--color-ground);
	}

	.column {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		width: min(760px, 100%);
		margin-inline: auto;
	}
</style>
