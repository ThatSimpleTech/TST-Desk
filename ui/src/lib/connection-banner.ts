// Connection-pill copy (TD-1304). Pure so the "gave up" case is testable
// without mounting the banner: after MAX_RESTARTS the host emits `stopped`
// while the client is still looping on getDaemonInfo, and that must not
// read as "Connecting…" forever.

export type BannerWs = "disconnected" | "connecting" | "connected" | "reconnecting" | "stopped";
export type BannerDaemon = "starting" | "connected" | "crashed" | "stopping" | "stopped";

/** How long a phone retries its saved computer before the banner says it
 *  can't reach it and offers to pair again (TD-5014). */
export const UNREACHABLE_AFTER_MS = 60_000;

/** A phone's view of its saved computer (TD-5014). `note` is a handshake
 *  message that is not a refusal (the computer couldn't read its devices),
 *  shown while the client keeps retrying; `unreachable` is set after
 *  UNREACHABLE_AFTER_MS of failed reconnects. */
export interface BannerRemote {
	unreachable?: boolean;
	note?: string | null;
}

/** `desktopName` is the paired computer a phone is retrying (TD-5014): a
 *  network failure keeps the saved target and says who it is waiting for. */
export function bannerLabel(
	ws: BannerWs,
	daemon: BannerDaemon,
	desktopName: string | null = null,
	remote: BannerRemote = {},
): string {
	if (ws === "connected") return "Connected";
	// Host gave up (or is stopping) while the socket never attached.
	if (
		(daemon === "stopped" || daemon === "stopping") &&
		(ws === "connecting" || ws === "reconnecting" || ws === "disconnected")
	) {
		return daemon === "stopping" ? "Daemon stopped" : "Couldn’t start the daemon";
	}
	if (ws !== "stopped") {
		if (remote.note) return remote.note;
		if (remote.unreachable) return `Can’t reach ${desktopName ?? "the computer"}`;
	}
	if (ws === "connecting" || ws === "reconnecting") {
		return desktopName === null ? "Connecting…" : `Connecting to ${desktopName}…`;
	}
	if (ws === "disconnected") {
		if (daemon === "crashed") return "Daemon crashed — reconnecting";
		return "Not connected";
	}
	return "Stopped";
}

/** True when the banner should offer "Pair again": a phone that has been
 *  failing to reach its saved computer for a while. Not while the computer
 *  answers with a note: it is reachable, and pairing would fail the same way. */
export function bannerOffersPairAgain(ws: BannerWs, remote: BannerRemote): boolean {
	return ws !== "connected" && ws !== "stopped" && remote.unreachable === true && !remote.note;
}

export function bannerTone(
	ws: BannerWs,
	daemon: BannerDaemon,
	remote: BannerRemote = {},
): "success" | "info" | "warning" {
	if (ws === "connected") return "success";
	if (bannerLabel(ws, daemon) === "Couldn’t start the daemon") return "warning";
	if (ws !== "stopped" && (remote.note || remote.unreachable)) return "warning";
	if (ws === "connecting" || ws === "reconnecting") return "info";
	return "warning";
}
