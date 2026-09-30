//! Quit waits for the daemon process, then SIGKILLs only if it is still up.
//!
//! `RunEvent::Exit` can run while the supervisor is still delivering
//! `shutdown` (macOS `applicationWillTerminate` does not wait for the
//! async quit task). Killing there used to land before the daemon read
//! the frame. The socket stays open until the daemon closes it — that
//! close is `ws_server.stop()`, which runs after the shutdown-requested
//! log and the port-file release — or until [`SHUTDOWN_GRACE`] expires.
//! The leader wait is the host's direct child. For a PyInstaller onefile
//! sidecar that child is the bootloader; it leaves after the Python
//! process does.

use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::tungstenite::Message;

use super::daemon_pid::{kill_spawned_group, pid_is_alive};
use super::ClientWs;

/// How long the host waits after asking the daemon to stop before SIGKILL.
pub(crate) const SHUTDOWN_GRACE: Duration = Duration::from_secs(5);

/// The daemon's shutdown message. One spelling, shared with the tests.
pub(crate) const SHUTDOWN_FRAME: &str = r#"{"type":"shutdown"}"#;

/// What the quit path did with the process-group leader.
#[derive(Debug)]
pub(crate) enum Reap {
    /// Leader exited on its own. The group was not signalled.
    Exited { code: Option<i32> },
    /// Grace elapsed and the leader was still alive. The group was SIGKILLed.
    Killed,
}

/// What `RunEvent::Exit` should do with a still-recorded leader pid.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ExitAction {
    /// Leader is already gone.
    Noop,
    /// Shutdown was requested and the grace has not elapsed.
    Wait,
    /// No shutdown was requested, or the grace has elapsed.
    Kill,
}

/// `elapsed` is how long ago [`super::DaemonHandle::request_shutdown`] ran.
/// `None` means this was not an in-app quit. This helper then says kill.
/// A system quit does not use that arm; it asks the process to stop
/// first. See `system_exit`.
pub(crate) fn exit_action(alive: bool, elapsed: Option<Duration>, grace: Duration) -> ExitAction {
    if !alive {
        return ExitAction::Noop;
    }
    match elapsed {
        Some(elapsed) if elapsed < grace => ExitAction::Wait,
        _ => ExitAction::Kill,
    }
}

/// Send `shutdown` and keep the socket until the peer closes or `deadline`.
///
/// Dropping right after `flush` lets our FIN/RST win the race against the
/// text frame when the process is killed in the same second. The daemon
/// closes this socket from `stop()`, after it has logged the request and
/// released the port file. We do not call `close().await`: that handshake
/// is what used to outlast the grace (TD-4848).
pub(crate) async fn hold_until_peer_closes(mut ws: ClientWs, deadline: Instant) {
    let _ = ws.send(Message::Text(SHUTDOWN_FRAME.into())).await;
    let _ = ws.flush().await;
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return;
    }
    let _ = tokio::time::timeout(remaining, async {
        loop {
            match ws.next().await {
                Some(Ok(Message::Close(_))) | Some(Err(_)) | None => break,
                Some(Ok(_)) => {}
            }
        }
    })
    .await;
}

/// Deliver `shutdown`, then wait until the spawned leader is reaped.
/// SIGKILL only if it is still running at `grace`.
pub(crate) async fn shutdown_and_reap(
    ws: ClientWs,
    child: &mut tokio::process::Child,
    leader_pid: u32,
    grace: Duration,
) -> Reap {
    let deadline = Instant::now() + grace;
    hold_until_peer_closes(ws, deadline).await;
    wait_spawned(child, leader_pid, deadline).await
}

/// Same delivery for a daemon the host attached to instead of spawned.
pub(crate) async fn shutdown_attached(ws: ClientWs, leader_pid: u32, grace: Duration) -> Reap {
    let deadline = Instant::now() + grace;
    hold_until_peer_closes(ws, deadline).await;
    wait_pid(leader_pid, deadline).await
}

/// Block the `RunEvent::Exit` caller until the leader is gone or `deadline`.
/// SIGKILL only at the deadline. Returns whether this call SIGKILLed.
/// A zombie counts as gone: `kill -0` still succeeds, and a group kill
/// would hit a grandchild that is still flushing.
///
/// `thread::sleep` is deliberate. This runs on the main thread from
/// `applicationWillTerminate`, which must not `block_on` the runtime.
pub(crate) fn block_until_leader_gone(pid: u32, deadline: Instant) -> bool {
    while Instant::now() < deadline {
        if !leader_still_running(pid) {
            return false;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    if leader_still_running(pid) {
        kill_spawned_group(pid);
        return true;
    }
    false
}

async fn wait_spawned(
    child: &mut tokio::process::Child,
    leader_pid: u32,
    deadline: Instant,
) -> Reap {
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return kill_if_running(leader_pid, child).await;
    }
    match tokio::time::timeout(remaining, child.wait()).await {
        Ok(Ok(status)) => Reap::Exited {
            code: status.code(),
        },
        Ok(Err(e)) => {
            log::warn!("daemon wait errored after shutdown: {e}");
            if leader_still_running(leader_pid) {
                kill_spawned_group(leader_pid);
                let _ = child.wait().await;
                Reap::Killed
            } else {
                Reap::Exited { code: None }
            }
        }
        Err(_elapsed) => kill_if_running(leader_pid, child).await,
    }
}

async fn kill_if_running(leader_pid: u32, child: &mut tokio::process::Child) -> Reap {
    if leader_still_running(leader_pid) {
        kill_spawned_group(leader_pid);
        let _ = child.wait().await;
        Reap::Killed
    } else {
        let status = child.wait().await.ok();
        Reap::Exited {
            code: status.and_then(|s| s.code()),
        }
    }
}

async fn wait_pid(leader_pid: u32, deadline: Instant) -> Reap {
    loop {
        if !leader_still_running(leader_pid) {
            return Reap::Exited { code: None };
        }
        if Instant::now() >= deadline {
            kill_spawned_group(leader_pid);
            return Reap::Killed;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

pub(crate) fn leader_still_running(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    #[cfg(unix)]
    {
        if let Some(state) = unix_process_state(pid) {
            // `Z` is exited but not reaped. Killing the group then hits
            // whatever child the bootloader has not waited out yet.
            return !state.is_empty() && !state.starts_with('Z');
        }
    }
    pid_is_alive(pid)
}

#[cfg(unix)]
fn unix_process_state(pid: u32) -> Option<String> {
    let output = std::process::Command::new("ps")
        .args(["-o", "stat=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    if !output.status.success() {
        return Some(String::new());
    }
    Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

#[cfg(test)]
#[path = "shutdown_tests.rs"]
mod process_tests;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exit_action_matches_quit_and_force() {
        let grace = Duration::from_secs(5);
        let cases = [
            (false, None, ExitAction::Noop),
            (false, Some(Duration::from_secs(1)), ExitAction::Noop),
            (false, Some(Duration::from_secs(9)), ExitAction::Noop),
            (true, None, ExitAction::Kill),
            (true, Some(Duration::from_secs(0)), ExitAction::Wait),
            (true, Some(Duration::from_secs(4)), ExitAction::Wait),
            (true, Some(grace), ExitAction::Kill),
            (true, Some(Duration::from_secs(6)), ExitAction::Kill),
        ];
        for (alive, elapsed, want) in cases {
            assert_eq!(
                exit_action(alive, elapsed, grace),
                want,
                "alive={alive} elapsed={elapsed:?}"
            );
        }
    }
}
