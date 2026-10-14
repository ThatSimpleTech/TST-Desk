// Connection-pill copy (TD-1304). Pure so the "gave up" case is testable
// without mounting the banner: after MAX_RESTARTS the host emits `stopped`
// while the client is still looping on getDaemonInfo, and that must not
// read as "Connecting…" forever.

export type BannerWs = "disconnected" | "connecting" | "connected" | "reconnecting" | "stopped";
export type BannerDaemon = "starting" | "connected" | "crashed" | "stopping" | "stopped";

/** `desktopName` is the paired computer a phone is retrying (TD-5014): a
 *  network failure keeps the saved target and says who it is waiting for. */
export function bannerLabel(ws: BannerWs, daemon: BannerDaemon, desktopName: string | null = null): string {
	if (ws === "connected") return "Connected";
	// Host gave up (or is stopping) while the socket never attached.
	if (
		(daemon === "stopped" || daemon === "stopping") &&
		(ws === "connecting" || ws === "reconnecting" || ws === "disconnected")
	) {
		return daemon === "stopping" ? "Daemon stopped" : "Couldn’t start the daemon";
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

export function bannerTone(ws: BannerWs, daemon: BannerDaemon): "success" | "info" | "warning" {
	if (ws === "connected") return "success";
	if (bannerLabel(ws, daemon) === "Couldn’t start the daemon") return "warning";
	if (ws === "connecting" || ws === "reconnecting") return "info";
	return "warning";
}
