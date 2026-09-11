// Jump-to-turn requests from the activity pane to the chat (navigation
// round, September 2026).
//
// A turn header in the Activity pane names the daemon's turn_id; the chat
// holds the user message that opened that turn under the same id
// (chat-events.ts). This is the one-way channel between them: the pane
// asks, the message list scrolls. A counter rather than a flag, so asking
// for the same turn twice scrolls twice.

export const chatJump = $state<{ turnId: string | null; nonce: number }>({
  turnId: null,
  nonce: 0,
});

export function jumpToTurn(turnId: string): void {
  chatJump.turnId = turnId;
  chatJump.nonce += 1;
}
