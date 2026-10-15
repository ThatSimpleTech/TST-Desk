// Release-updater store (TD-4837). The host emits `update-status`; this
// module holds that snapshot for the banner and Settings → About.

import { isTauri } from "./open-file";
import {
	type UpdateStatus,
} from "./update-status";

export const update = $state({
	status: null as UpdateStatus | null,
	dismissed: false,
});

function applyStatus(next: UpdateStatus): void {
	const prev = update.status;
	if (prev === null || prev.phase !== next.phase || prev.latest !== next.latest) {
		update.dismissed = false;
	}
	update.status = next;
}

export function dismissUpdate(): void {
	update.dismissed = true;
}

async function invokeStatus(command: string, args?: Record<string, unknown>): Promise<void> {
	if (!isTauri()) return;
	const { invoke } = await import("@tauri-apps/api/core");
	applyStatus(await invoke<UpdateStatus>(command, args));
}

export async function checkForUpdates(): Promise<void> {
	await invokeStatus("check_for_updates");
}

export async function downloadUpdate(): Promise<void> {
	await invokeStatus("download_update");
}

export async function restartToUpdate(): Promise<void> {
	await invokeStatus("restart_to_update");
}

export async function openUpdateInstaller(): Promise<void> {
	if (!isTauri()) return;
	const { invoke } = await import("@tauri-apps/api/core");
	await invoke("open_update_installer");
}

export async function setAutomaticUpdates(enabled: boolean): Promise<void> {
	await invokeStatus("set_automatic_updates", { enabled });
}

export async function openReleasePage(): Promise<void> {
	const url = update.status?.release_url;
	if (!url || !isTauri()) return;
	const { openUrl } = await import("@tauri-apps/plugin-opener");
	await openUrl(url);
}

/** Listen for the life of the window. Safe to call once per webview. */
export async function startUpdates(): Promise<() => void> {
	if (!isTauri()) return () => {};
	const { listen } = await import("@tauri-apps/api/event");
	const { invoke } = await import("@tauri-apps/api/core");
	const unlisten = await listen<UpdateStatus>("update-status", (event) => {
		applyStatus(event.payload);
	});
	try {
		applyStatus(await invoke<UpdateStatus>("update_status"));
	} catch {
		// The host snapshot is optional until the first event arrives.
	}
	return () => {
		unlisten();
	};
}
