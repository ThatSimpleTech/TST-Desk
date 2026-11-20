// TD-5235, TD-5236: what picking a model does, in one place. The picker's
// rows and the quick-switch shortcut (⌘⌥1…9, Ctrl+Alt+1…9) both come here,
// so a key press and a click mean the same thing.

import { isBridged } from "./bridged-id";
import { engineClash } from "./model-engine";
import { modelAtSlot } from "./model-sections";
import { session, switchModel, useForNewChats } from "./session-status.svelte.js";
import { newSession } from "./sessions.svelte.js";
import { settings } from "./settings.svelte.js";

export const BUSY = "Cannot change model while a turn is running";
export const AFTER_TURN = "Applies when this turn finishes";
export const NEW_CHAT = "Opens a new chat";

/** A chat on another computer switches through its hub, which keeps the
 *  older verb and its rules. */
function bridged(): boolean {
	return isBridged(session.sessionId);
}

/** TD-5239: `name` is on another engine than this chat's, so the chat
 *  cannot move to it. TD-5235: its row opens a new chat on it instead. */
export function opensNewChat(name: string): boolean {
	return (
		name !== session.preset &&
		engineClash(name, settings.presetEngines, session.engine) !== null
	);
}

/** The row cannot be used right now: another computer's chat, mid-turn. */
export function held(name: string): boolean {
	return name !== session.preset && session.turnActive && bridged();
}

/** Whether "Only this chat" is offered for `name`. */
export function offersOnlyHere(name: string): boolean {
	return name !== session.preset && !opensNewChat(name) && !bridged();
}

/** The row's tooltip: why it is held, or what the click will do. */
export function pickHint(name: string): string {
	if (name === session.preset) return "";
	const reason = engineClash(name, settings.presetEngines, session.engine);
	if (reason !== null) return `${NEW_CHAT}. ${reason}`;
	if (held(name)) return BUSY;
	return session.turnActive ? AFTER_TURN : "";
}

/** A click, or the model's shortcut: this chat moves to `name` and new
 *  chats use it too. A model on the other engine opens a new chat. */
export function pickModel(name: string): void {
	if (held(name)) return;
	if (opensNewChat(name)) {
		useForNewChats(name);
		newSession(name);
	} else if (name !== session.preset || name !== settings.activePreset) {
		switchModel(name, { onlyThisChat: false });
	}
}

/** "Only this chat": the model new chats use stays as it is. */
export function pickOnlyHere(name: string): void {
	if (offersOnlyHere(name)) switchModel(name, { onlyThisChat: true });
}

/** The quick-switch shortcut. False when no model has that slot. */
export function pickSlot(slot: number): boolean {
	const name = modelAtSlot(slot, settings.presetSlots);
	if (name === null) return false;
	pickModel(name);
	return true;
}
