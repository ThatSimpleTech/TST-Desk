// @vitest-environment jsdom
//
// TD-5279: what a paired phone may change is switched on at the computer,
// after its disclosure. These pin the disclosure's words (so they cannot be
// softened unnoticed), what the block sends, and what a phone's own pages
// show while a permission is off.

import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount, type Component } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, RemoteDeviceSummary } from "./protocol";

const mocks = vi.hoisted(() => ({ sent: [] as ClientMessageUnion[] }));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: () => () => undefined,
	sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
}));

import PhonePermissions from "./components/PhonePermissions.svelte";
import PairedDevices from "./components/PairedDevices.svelte";
import SettingsMcp from "./components/SettingsMcp.svelte";
import PolicyRuleList from "./components/PolicyRuleList.svelte";
import DoctorPane from "./components/DoctorPane.svelte";
import { settings, type McpServerRow } from "./settings.svelte.js";
import { doctor, buildDoctorReport } from "./doctor.svelte.js";
import { banners, clearNotifications, notifyEvent, toasts } from "./notifications.svelte.js";
import {
	PHONE_PERMISSIONS,
	isGranted,
	originWords,
	permissionCopy,
	phoneChangeNotice,
	stateWords,
} from "./phone-permissions";

let component: ReturnType<typeof mount> | null = null;

function show(what: Component<any>, props: Record<string, unknown> = {}): HTMLElement {
	const target = document.createElement("div");
	document.body.appendChild(target);
	component = mount(what, { target, props });
	flushSync();
	return target;
}

function press(root: ParentNode, label: string): void {
	const button = [...root.querySelectorAll("button")].find(
		(b) => (b.getAttribute("aria-label") ?? b.textContent?.trim()) === label,
	);
	if (!button) throw new Error(`no button "${label}"`);
	button.click();
	flushSync();
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = "";
	mocks.sent = [];
	settings.phonePermissions = {};
	settings.remoteDevices = [];
	settings.mcpServers = [];
	doctor.open = false;
	doctor.checks = [];
	clearNotifications();
});

const NOW = Date.parse("2026-11-20T18:00:00Z");
const TITLE = "Let paired phones add and change MCP servers";

describe("the words", () => {
	it("the MCP disclosure says what it lets a phone do, in full", () => {
		expect(permissionCopy("mcp_servers")).toEqual({
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
		});
	});

	it("the approvals disclosure says what it lets a phone do, in full", () => {
		expect(permissionCopy("approval_switches")).toEqual({
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
		});
	});

	it("the models disclosure says where the conversation and the key go (TD-5296)", () => {
		const copy = permissionCopy("models_and_keys");
		expect(copy?.title).toBe("Let paired phones add models and keys");
		expect(copy?.lead).toContain("the key itself, then go to the server that entry names");
		expect(copy?.points).toContain(
			"A phone can always switch between the models already here; that needs no permission.",
		);
	});

	it("the computer-use disclosure says a phone can never release a Stop (TD-5297)", () => {
		const copy = permissionCopy("computer_use");
		expect(copy?.title).toBe("Let paired phones change computer use");
		expect(copy?.points).toContain(
			"No phone can release a Stop, even with this on. Only the computer can.",
		);
	});

	it("lists exactly the permissions the daemon knows", () => {
		expect(PHONE_PERMISSIONS.map((p) => p.capability)).toEqual([
			"mcp_servers",
			"approval_switches",
			"models_and_keys",
			"computer_use",
		]);
	});

	it("every permission has a full disclosure", () => {
		for (const p of PHONE_PERMISSIONS) {
			expect(p.title.length).toBeGreaterThan(10);
			expect(p.lead).toContain("With this on");
			expect(p.points.length).toBeGreaterThanOrEqual(2);
			expect(p.offOnPhone).toContain("on the computer");
		}
	});

	it("absent, off, unreadable and expired all read as off", () => {
		expect(isGranted({}, "mcp_servers", NOW)).toBe(false);
		expect(isGranted({ mcp_servers: { enabled: false } }, "mcp_servers", NOW)).toBe(false);
		expect(isGranted({ other: { enabled: true } }, "mcp_servers", NOW)).toBe(false);
		const timed = { mcp_servers: { enabled: true, expires_at: "2026-11-20T19:00:00Z" } };
		expect(isGranted(timed, "mcp_servers", NOW)).toBe(true);
		expect(isGranted(timed, "mcp_servers", NOW + 3_600_000)).toBe(false);
		const broken = { mcp_servers: { enabled: true, expires_at: "soon" } };
		expect(isGranted(broken, "mcp_servers", NOW)).toBe(false);
		expect(isGranted({ mcp_servers: { enabled: true, expires_at: null } }, "mcp_servers", NOW)).toBe(true);
	});

	it("says how long it is on for", () => {
		expect(stateWords({}, "mcp_servers", NOW)).toBe("Off");
		expect(stateWords({ mcp_servers: { enabled: true } }, "mcp_servers", NOW)).toBe(
			"On until you turn it off",
		);
		const timed = { mcp_servers: { enabled: true, expires_at: "2026-11-20T19:00:00Z" } };
		expect(stateWords(timed, "mcp_servers", NOW)).toMatch(/^On until \S/);
		expect(stateWords(timed, "mcp_servers", NOW + 3_600_000)).toBe("Off");
	});

	it("says which phone a server came from", () => {
		expect(originWords("", "")).toBe("");
		expect(originWords(undefined, undefined)).toBe("");
		expect(originWords("Pixel 8", "not a date")).toBe("Added or changed from Pixel 8");
		expect(originWords("Pixel 8", "2026-11-20T18:02:11Z")).toMatch(
			/^Added or changed from Pixel 8 on .*20/,
		);
	});
});

describe("Settings → Devices, what paired phones may change", () => {
	it("lists the permission as off and sends nothing until the disclosure is answered", () => {
		const root = show(PhonePermissions);
		expect(root.textContent).toContain("What paired phones may change");
		expect(root.querySelector(".state")?.textContent?.trim()).toBe("Off");
		expect(root.querySelector('[role="dialog"]')).toBeNull();
		press(root, `Turn on: ${TITLE}`);
		expect(mocks.sent).toEqual([]);
		const dialog = root.querySelector('[role="dialog"]');
		expect(dialog).not.toBeNull();
		const text = dialog?.textContent ?? "";
		const copy = permissionCopy("mcp_servers");
		expect(text).toContain(copy?.lead);
		for (const point of copy?.points ?? []) expect(text).toContain(point);
		expect(text).toContain(copy?.advice);
		const buttons = [...(dialog?.querySelectorAll("button") ?? [])].map((b) => b.textContent?.trim());
		expect(buttons).toEqual(["Keep off", "Turn on for 1 hour", "Turn on until I turn it off"]);
	});

	it("Keep off sends nothing and closes", () => {
		const root = show(PhonePermissions);
		press(root, `Turn on: ${TITLE}`);
		press(root, "Keep off");
		expect(mocks.sent).toEqual([]);
		expect(root.querySelector('[role="dialog"]')).toBeNull();
	});

	it("Escape is Keep off", () => {
		const root = show(PhonePermissions);
		press(root, `Turn on: ${TITLE}`);
		root
			.querySelector('[role="dialog"]')
			?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
		flushSync();
		expect(mocks.sent).toEqual([]);
		expect(root.querySelector('[role="dialog"]')).toBeNull();
	});

	it("turns it on for an hour, or until turned off", () => {
		const root = show(PhonePermissions);
		press(root, `Turn on: ${TITLE}`);
		press(root, "Turn on for 1 hour");
		press(root, `Turn on: ${TITLE}`);
		press(root, "Turn on until I turn it off");
		expect(mocks.sent).toEqual([
			{ type: "set_phone_permission", capability: "mcp_servers", enabled: true, expires_in_seconds: 3600 },
			{ type: "set_phone_permission", capability: "mcp_servers", enabled: true },
		]);
	});

	it("shows it on and turns it off in one press", () => {
		settings.phonePermissions = { mcp_servers: { enabled: true, expires_at: null } };
		const root = show(PhonePermissions);
		expect(root.querySelector(".state--on")?.textContent?.trim()).toBe("On until you turn it off");
		press(root, `Turn off: ${TITLE}`);
		expect(root.querySelector('[role="dialog"]')).toBeNull();
		expect(mocks.sent).toEqual([
			{ type: "set_phone_permission", capability: "mcp_servers", enabled: false },
		]);
	});
});

const SERVERS = [
	{
		id: "probe",
		transport: "stdio",
		command: ["/usr/bin/touch", "/tmp/x"],
		url: "",
		enabled: true,
		originDevice: "Pixel 8",
		originAt: "2026-11-20T18:02:11Z",
		excludedTools: [],
		values: [],
	},
	{ id: "mine", transport: "stdio", command: ["true"], url: "", enabled: true, excludedTools: [], values: [] },
] as unknown as McpServerRow[];

describe("Settings → MCP servers", () => {
	it("off the computer with the permission off: the list, the reason, nothing to press", () => {
		settings.mcpServers = SERVERS;
		const root = show(SettingsMcp, { canManage: false });
		expect(root.querySelectorAll("button")).toHaveLength(0);
		expect(root.querySelectorAll("input")).toHaveLength(0);
		expect(root.textContent).toContain(
			"Adding or changing MCP servers from a phone is turned off. You can turn it on under Settings, Phones and devices on the computer.",
		);
		expect(root.textContent).toContain("/usr/bin/touch /tmp/x");
	});

	it("marks a server that came from a phone, and only that one", () => {
		settings.mcpServers = SERVERS;
		const root = show(SettingsMcp, { canManage: true });
		const marks = [...root.querySelectorAll(".origin")].map((el) => el.textContent?.trim() ?? "");
		expect(marks).toHaveLength(1);
		expect(marks[0]).toMatch(/^Added or changed from Pixel 8 on /);
	});
});

const PHONE: RemoteDeviceSummary = {
	id: "dev_1",
	name: "Pixel 8",
	paired_at: "2026-11-01T10:00:00Z",
	last_seen: null,
	connected: false,
	kind: "phone",
	mcp_servers_added: ["probe", "walk"],
};

describe("removing a phone", () => {
	it("lists what it added and leaves it unless asked", () => {
		settings.remoteDevices = [PHONE];
		const root = show(PairedDevices, { canManage: true });
		mocks.sent = [];
		press(root, "Remove Pixel 8");
		expect((root.textContent ?? "").replace(/\s+/g, " ")).toContain(
			"This phone added or changed MCP servers that stay after it is removed: probe, walk.",
		);
		const box = root.querySelector<HTMLInputElement>('input[type="checkbox"]');
		expect(box?.checked).toBe(false);
		press(root, "Remove");
		expect(mocks.sent).toEqual([{ type: "remove_remote_device", device_id: "dev_1" }]);
	});

	it("takes them with it when the box is ticked", () => {
		settings.remoteDevices = [PHONE];
		const root = show(PairedDevices, { canManage: true });
		mocks.sent = [];
		press(root, "Remove Pixel 8");
		root.querySelector<HTMLInputElement>('input[type="checkbox"]')?.click();
		flushSync();
		press(root, "Remove");
		expect(mocks.sent).toEqual([
			{ type: "remove_remote_device", device_id: "dev_1", remove_added: true },
		]);
	});

	it("asks nothing extra about a phone that added nothing", () => {
		settings.remoteDevices = [{ ...PHONE, mcp_servers_added: [] }];
		const root = show(PairedDevices, { canManage: true });
		mocks.sent = [];
		press(root, "Remove Pixel 8");
		expect(root.querySelector('input[type="checkbox"]')).toBeNull();
		press(root, "Remove");
		expect(mocks.sent).toEqual([{ type: "remove_remote_device", device_id: "dev_1" }]);
	});

	it("the box does not carry over from one phone to the next", () => {
		settings.remoteDevices = [PHONE, { ...PHONE, id: "dev_2", name: "Tablet", mcp_servers_added: ["x"] }];
		const root = show(PairedDevices, { canManage: true });
		mocks.sent = [];
		press(root, "Remove Pixel 8");
		root.querySelector<HTMLInputElement>('input[type="checkbox"]')?.click();
		flushSync();
		press(root, "Keep");
		press(root, "Remove Tablet");
		expect(root.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(false);
	});
});

describe("Settings → Policy, the skip-all switch", () => {
	function skipSwitch(root: ParentNode): HTMLButtonElement {
		const found = root.querySelector<HTMLButtonElement>('button[role="switch"]');
		if (!found) throw new Error("no switch");
		return found;
	}

	it("on a phone without the permission it cannot be turned on, and says where to allow it", () => {
		settings.skipAllApprovals = false;
		const root = show(PolicyRuleList, { sessionId: null, canSkipAll: false });
		const button = skipSwitch(root);
		expect(button.disabled).toBe(true);
		button.click();
		flushSync();
		expect(mocks.sent).toEqual([]);
		expect(root.textContent).toContain(
			"Turning approvals off from a phone is turned off. You can turn it on under Settings, Phones and devices on the computer.",
		);
	});

	it("a phone can always turn approvals back on", () => {
		settings.skipAllApprovals = true;
		const root = show(PolicyRuleList, { sessionId: null, canSkipAll: false });
		const button = skipSwitch(root);
		expect(button.disabled).toBe(false);
		expect(root.textContent).not.toContain("from a phone is turned off");
		button.click();
		flushSync();
		expect(mocks.sent).toEqual([{ type: "set_skip_all_approvals", enabled: false }]);
		settings.skipAllApprovals = false;
	});

	it("on the computer, and on a phone that was allowed, it works as before", () => {
		settings.skipAllApprovals = false;
		const root = show(PolicyRuleList, { sessionId: null });
		const button = skipSwitch(root);
		expect(button.disabled).toBe(false);
		expect(root.textContent).not.toContain("from a phone is turned off");
		button.click();
		flushSync();
		expect(mocks.sent).toEqual([{ type: "set_skip_all_approvals", enabled: true }]);
	});
});

describe("when a phone uses a permission", () => {
	const event = {
		type: "phone_change",
		seq: 1,
		device_name: "Pixel 8",
		capability: "mcp_servers",
		summary: "added MCP server 'probe': /usr/bin/touch /tmp/x",
	} as const;

	it("the computer's window keeps a banner naming the phone and the change", () => {
		expect(phoneChangeNotice(event)).toEqual({
			severity: "banner",
			title: "Pixel 8 changed this computer",
			body: "Pixel 8 added MCP server 'probe': /usr/bin/touch /tmp/x",
		});
		notifyEvent(event as DaemonEventUnion);
		expect(toasts).toHaveLength(0);
		expect(banners.map((b) => b.body)).toEqual([
			"Pixel 8 added MCP server 'probe': /usr/bin/touch /tmp/x",
		]);
		// A second, different change is a second banner; the same one is not.
		notifyEvent({ ...event, summary: "removed MCP server 'probe'." } as DaemonEventUnion);
		notifyEvent(event as DaemonEventUnion);
		expect(banners).toHaveLength(2);
	});

	it("a refusal on the phone says where the switch is", () => {
		notifyEvent({
			type: "error",
			code: "phone_permission_off",
			message: "Turn on 'MCP servers' under Settings, Phones and devices on the computer to do this from a phone.",
		} as DaemonEventUnion);
		expect(toasts.map((t) => [t.title, t.body])).toEqual([
			[
				"Turned off for phones",
				"Turn on 'MCP servers' under Settings, Phones and devices on the computer to do this from a phone.",
			],
		]);
	});
});

describe("Doctor", () => {
	const row = {
		name: "phone_permissions",
		status: "warn",
		detail: "paired phones may change: MCP servers ON (until turned off)",
	} as const;

	it("shows a phone permission left on as a warning, in the pane and the copied report", () => {
		doctor.open = true;
		doctor.checks = [row];
		const root = show(DoctorPane);
		expect(root.querySelector(".mark--warn")).not.toBeNull();
		expect(root.textContent).toContain("MCP servers ON (until turned off)");
		expect(buildDoctorReport([row])).toContain(
			"! phone_permissions — paired phones may change: MCP servers ON (until turned off)",
		);
	});
});
