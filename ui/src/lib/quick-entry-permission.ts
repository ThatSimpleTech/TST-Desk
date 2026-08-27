// Accessibility notice when the quick-entry shortcut cannot register (TD-4702).

import { notify } from "./notifications.svelte.js";

export const QUICK_ENTRY_PERMISSION_EVENT = "quick-entry-permission";

export async function startQuickEntryPermission(): Promise<() => void> {
	try {
		const { listen } = await import("@tauri-apps/api/event");
		return listen<{ title?: string; body?: string; url?: string }>(
			QUICK_ENTRY_PERMISSION_EVENT,
			(ev) => {
				const payload = ev.payload;
				notify("quick-entry-permission", {
					severity: "banner",
					title: payload.title ?? "Quick entry needs Accessibility",
					body:
						payload.body ??
						"In System Settings → Privacy & Security → Accessibility, allow TST Desk, then restart.",
				});
			},
		);
	} catch {
		return () => {};
	}
}
