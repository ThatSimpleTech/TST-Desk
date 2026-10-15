// Host snapshot for the release updater (TD-4837). The window renders
// these fields; it does not decide whether an update exists.

export type UpdatePhase =
	| "idle"
	| "checking"
	| "up_to_date"
	| "available"
	| "downloading"
	| "ready"
	| "error";

export interface UpdateStatus {
	phase: UpdatePhase;
	current: string;
	latest: string | null;
	release_url: string | null;
	detail: string;
	automatic: boolean;
	can_apply: boolean;
	can_open_installer: boolean;
	announce: boolean;
}

export type UpdateAction = "download" | "restart" | "open" | "retry" | "releases";

export function bannerVisible(status: UpdateStatus | null, dismissed: boolean): boolean {
	if (dismissed || status === null || !status.announce) return false;
	return (
		status.phase === "available" ||
		status.phase === "downloading" ||
		status.phase === "ready" ||
		status.phase === "error"
	);
}

export function primaryAction(status: UpdateStatus): UpdateAction | null {
	switch (status.phase) {
		case "available":
			if (status.can_apply || status.can_open_installer) return "download";
			return status.release_url ? "releases" : null;
		case "ready":
			if (status.can_apply) return "restart";
			if (status.can_open_installer) return "open";
			return status.release_url ? "releases" : null;
		case "error":
			return "retry";
		default:
			return null;
	}
}

export function actionLabel(action: UpdateAction): string {
	switch (action) {
		case "download":
			return "Download";
		case "restart":
			return "Restart to update";
		case "open":
			return "Open installer";
		case "retry":
			return "Try again";
		case "releases":
			return "Open releases page";
	}
}

/** The version in the title is the host's `latest`, not a guess. */
export function bannerTitle(status: UpdateStatus): string {
	const version = status.latest;
	switch (status.phase) {
		case "ready":
			return version ? `TST Desk ${version} is ready` : "Update ready";
		case "downloading":
			return version ? `Downloading TST Desk ${version}` : "Downloading update";
		case "available":
			return version ? `TST Desk ${version} is available` : "Update available";
		case "error":
			return "Update didn't finish";
		default:
			return "";
	}
}
