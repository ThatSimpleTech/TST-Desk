// What a paired phone may change (TD-5279): the words, and the small
// decisions around them. No store and no DOM, so the wording is testable
// on its own: it is the disclosure a person reads before switching a
// permission on, and a softer sentence here would be a weaker warning.

import type { NoticeSpec } from "./error-copy";
import type { PhoneChange, PhonePermissionState } from "./protocol";

export interface PhonePermissionCopy {
	capability: string;
	/** The row's name on the Devices page, and the dialog's heading. */
	title: string;
	/** What it is, and the plainest statement of what switching it on allows. */
	lead: string;
	/** What stays true afterwards. Each is something a person would not guess. */
	points: string[];
	/** When it is reasonable to turn on. */
	advice: string;
	/** What a phone's own page says while this is off. */
	offOnPhone: string;
}

export const ONE_HOUR_SECONDS = 3600;

export const PHONE_PERMISSIONS: PhonePermissionCopy[] = [
	{
		capability: "mcp_servers",
		title: "Let paired phones add and change MCP servers",
		lead:
			"An MCP server is a program this computer starts and keeps running. With this on, " +
			"anyone holding a paired phone can make this computer run any command as you, " +
			"immediately and without an approval prompt here.",
		points: [
			"Servers added from a phone stay after you remove the phone. Removing a phone does not undo what it changed.",
			"These programs run outside the approval system and are not sandboxed.",
			"Turning this off later stops new changes; it does not remove servers already added.",
		],
		advice: "Leave this off unless you need to manage MCP servers while away from the computer.",
		offOnPhone:
			"Adding or changing MCP servers from a phone is turned off. You can turn it on under " +
			"Settings, Phones and devices on the computer.",
	},
	{
		capability: "approval_switches",
		title: "Let paired phones turn approvals off",
		lead:
			"With this on, a paired phone can turn off approval prompts for every session on this " +
			"computer. Commands and file changes then run without anyone confirming them, " +
			"including ones outside the project folder.",
		points: [
			"The setting stays on until someone turns it off, even after the phone is removed.",
			"A phone can already approve one request at a time. This is the same power with nobody looking at each request.",
			"A phone can always turn approval prompts back on; that needs no permission.",
		],
		advice:
			"Leave this off unless you run long jobs you trust and need to unblock them while away.",
		offOnPhone:
			"Turning approvals off from a phone is turned off. You can turn it on under " +
			"Settings, Phones and devices on the computer.",
	},
];

export function permissionCopy(capability: string): PhonePermissionCopy | undefined {
	return PHONE_PERMISSIONS.find((p) => p.capability === capability);
}

/** May a phone use it right now? Unknown, absent and expired all read as no. */
export function isGranted(
	permissions: Record<string, PhonePermissionState>,
	capability: string,
	now: number,
): boolean {
	const state = permissions[capability];
	if (state === undefined || state.enabled !== true) return false;
	if (state.expires_at === null || state.expires_at === undefined) return true;
	const until = Date.parse(state.expires_at);
	return Number.isFinite(until) && now < until;
}

/** "Off", "On until you turn it off", or "On until 19:00". */
export function stateWords(
	permissions: Record<string, PhonePermissionState>,
	capability: string,
	now: number,
): string {
	if (!isGranted(permissions, capability, now)) return "Off";
	const expires = permissions[capability]?.expires_at;
	if (expires === null || expires === undefined) return "On until you turn it off";
	const until = new Date(expires);
	const sameDay = new Date(now).toDateString() === until.toDateString();
	const time = until.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
	return sameDay ? `On until ${time}` : `On until ${until.toLocaleDateString()} ${time}`;
}

/** "Added or changed from Pixel 8 on 20 Nov", or "" for the computer's own. */
export function originWords(device: string | undefined, at: string | undefined): string {
	if (!device) return "";
	const when = at ? new Date(at) : null;
	if (when === null || Number.isNaN(when.getTime())) return `Added or changed from ${device}`;
	const day = when.toLocaleDateString([], { day: "numeric", month: "short" });
	return `Added or changed from ${device} on ${day}`;
}

/**
 * What the computer's window shows when a phone used a permission. A banner,
 * not a toast: the person may be away from the screen when it happens, and
 * it stays until they dismiss it.
 */
export function phoneChangeNotice(event: PhoneChange): NoticeSpec {
	return {
		severity: "banner",
		title: `${event.device_name} changed this computer`,
		body: `${event.device_name} ${event.summary}`,
	};
}
