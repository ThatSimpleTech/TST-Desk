// "Pair a phone" dialog state (TD-5014).
//
// The desktop asks the daemon for a code and shows it until a phone redeems
// it, it expires, or the user cancels. The code lives in the daemon's memory
// and in this state while the dialog is open; nothing here is persisted.
// Fed by the settings reducer, so it rides the same event subscription.

import { sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, PairingCode, RemoteDeviceSummary } from "./protocol";

/** How long "Paired with …" stays up before the dialog closes itself. */
export const PAIRED_CLOSE_MS = 2000;

const NOT_CONNECTED = "TST Desk isn't connected to its background service. Try again in a moment.";

export const pairDialog = $state({
	open: false,
	/** start_pairing sent, no code back yet. */
	waiting: false,
	offer: null as PairingCode | null,
	error: null as string | null,
	paired: null as RemoteDeviceSummary | null,
});

let closeTimer: ReturnType<typeof setTimeout> | null = null;

function clearCloseTimer(): void {
	if (closeTimer !== null) clearTimeout(closeTimer);
	closeTimer = null;
}

export function openPairDialog(): void {
	clearCloseTimer();
	pairDialog.open = true;
	pairDialog.paired = null;
	requestPairingCode();
}

/** The first ask, or "New code" after an expiry. A new start replaces the old code. */
export function requestPairingCode(): void {
	pairDialog.offer = null;
	pairDialog.error = null;
	pairDialog.waiting = true;
	if (!sendToDaemon({ type: "start_pairing" })) {
		pairDialog.waiting = false;
		pairDialog.error = NOT_CONNECTED;
	}
}

/** Cancel tells the daemon too, so the code cannot be redeemed after the dialog is gone. */
export function cancelPairDialog(): void {
	if (pairDialog.offer !== null || pairDialog.waiting) sendToDaemon({ type: "cancel_pairing" });
	closePairDialog();
}

export function closePairDialog(): void {
	clearCloseTimer();
	pairDialog.open = false;
	pairDialog.waiting = false;
	pairDialog.offer = null;
	pairDialog.error = null;
	pairDialog.paired = null;
}

/** Forget one phone. Acked with setup_state, which redraws the list. */
export function removeRemoteDevice(deviceId: string): void {
	sendToDaemon({ type: "remove_remote_device", device_id: deviceId });
}

/** Ask for the device list now. The daemon pushes presence after that, so
 *  "connected" and "last seen" stay live while the list is open. */
export function refreshRemoteDevices(): void {
	sendToDaemon({ type: "get_setup_state" });
}

export function reducePairing(event: DaemonEventUnion): void {
	if (event.type === "pairing_code") {
		// Starting may have turned remote attach on, and neither the code nor
		// its errors carry a request id, so read the settings back.
		refreshRemoteDevices();
		// Only the window that asked shows a code.
		if (!pairDialog.open) return;
		pairDialog.offer = event;
		pairDialog.waiting = false;
		pairDialog.error = null;
		return;
	}
	if (event.type === "pairing_completed") {
		// Every loopback window hears this; each refreshes its device list.
		refreshRemoteDevices();
		if (!pairDialog.open || pairDialog.offer === null) return;
		pairDialog.paired = event.device;
		pairDialog.offer = null;
		clearCloseTimer();
		closeTimer = setTimeout(closePairDialog, PAIRED_CLOSE_MS);
		return;
	}
	// start_pairing has no request id, so a machine-wide error that lands
	// while we wait for a code (or names pairing) is the answer to it.
	// TD-5015: an error another computer sent (tagged `machine`) is not this
	// dialog's answer, even though it has no session id.
	if (event.type === "error" && pairDialog.open && (event.session_id ?? null) === null && !event.machine) {
		if (pairDialog.waiting || event.code.startsWith("pairing_")) {
			pairDialog.waiting = false;
			pairDialog.error = event.message;
			refreshRemoteDevices();
		}
	}
}
