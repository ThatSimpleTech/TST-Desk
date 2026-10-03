// Resume a session the daemon parked (a cap, or the progress guard).
// The banner is the control. Dismissing the banner only hides it.

import type { ClientMessageUnion } from "./protocol";

export function resumePausedSession(
	sessionId: string | null,
	state: string,
	send: (msg: ClientMessageUnion) => boolean,
): boolean {
	if (state !== "paused" || !sessionId) return false;
	return send({ type: "resume", session_id: sessionId });
}
