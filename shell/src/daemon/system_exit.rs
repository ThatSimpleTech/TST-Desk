//! System quit: Dock, AppleScript, logout, restart, shutdown.
//!
//! tao 0.35 on macOS implements `applicationWillTerminate` and not
//! `applicationShouldTerminate`. Those quits arrive as `RunEvent::Exit`
//! with no `RunEvent::ExitRequested`, so the in-app quit path never
//! runs. `applicationWillTerminate` is on the main thread, and the
//! process is already leaving. Driving the shutdown through tokio
//! (`block_on`, or waiting for the supervisor task) can deadlock: on a
//! current-thread runtime the blocked caller is the thread that would
//! have to poll the task. The frame is written on a duplicated fd of
//! the connection the supervisor already holds, and the reap is
//! `thread::sleep` plus `ps`. Neither needs the runtime.
//!
//! The fd stays open until this function returns. Dropping it first is
//! what used to let a close beat the shutdown frame (TD-4849). A second
//! websocket would be a new handshake, not that connection.
//!
//! One deadline covers the daemon and the embeddings sidecar. Embeddings
//! has no shutdown frame, so it is SIGTERMed and then uses whatever time
//! is left. A pid that is already gone is not waited on, so a sidecar
//! that has exited does not hold logout open.

use std::io::Write;
use std::net::TcpStream;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use super::daemon_pid::term_spawned_group;
use super::embeddings::EmbeddingsHandle;
use super::shutdown::{block_until_leader_gone, leader_still_running, SHUTDOWN_FRAME};
use super::DaemonHandle;

/// Logout must not sit on the in-app 5s grace. The daemon is asked
/// first; embeddings gets only what remains.
pub(crate) const SYSTEM_EXIT_BUDGET: Duration = Duration::from_secs(3);

/// Duplicated client socket the main thread can write without tokio.
pub(crate) struct ExitSocketSlot {
    inner: Mutex<Option<TcpStream>>,
}

impl ExitSocketSlot {
    pub(crate) fn new() -> Self {
        Self {
            inner: Mutex::new(None),
        }
    }

    pub(crate) fn store(&self, socket: Option<TcpStream>) {
        *self.inner.lock().unwrap() = socket;
    }

    pub(crate) fn take(&self) -> Option<TcpStream> {
        self.inner.lock().unwrap().take()
    }
}

/// `RunEvent::Exit`. An in-app quit already recorded
/// [`DaemonHandle::request_shutdown`] and is waited out by
/// [`super::best_effort_kill`]. Anything else is a system quit.
pub(crate) fn on_host_exit(daemon: &DaemonHandle, embeddings: &EmbeddingsHandle) {
    if daemon.shutdown_started.lock().unwrap().is_some() {
        super::best_effort_kill(daemon);
        super::embeddings::best_effort_kill(embeddings);
        return;
    }
    // Flag before the signal. The supervisor must not treat the exit as
    // a crash and spawn a replacement while this thread is still reaping.
    daemon.claim_system_exit();
    embeddings.claim_system_exit();
    reap_on_system_exit(
        daemon.exit_socket.take(),
        daemon.child_pid(),
        embeddings.child_pid(),
        SYSTEM_EXIT_BUDGET,
    );
}

/// Ask the daemon to stop, then the embeddings sidecar, inside `budget`.
/// SIGKILL only for a pid still running at the deadline.
pub(crate) fn reap_on_system_exit(
    socket: Option<TcpStream>,
    daemon_pid: Option<u32>,
    embeddings_pid: Option<u32>,
    budget: Duration,
) {
    let deadline = Instant::now() + budget;
    if let Some(pid) = daemon_pid {
        reap_daemon(socket, pid, deadline);
    }
    if let Some(pid) = embeddings_pid {
        reap_signalled(pid, deadline);
    }
}

/// Copy of the live client socket. The supervisor keeps the original.
pub(crate) fn duplicate_client_socket(ws: &super::ClientWs) -> Option<TcpStream> {
    duplicate_tcp(ws.get_ref().get_ref())
}

fn reap_daemon(mut socket: Option<TcpStream>, pid: u32, deadline: Instant) {
    if !leader_still_running(pid) {
        return;
    }
    let wrote = socket
        .as_mut()
        .is_some_and(|stream| write_shutdown_frame(stream, deadline));
    if !wrote {
        // TD-4842: SIGTERM runs the same graceful shutdown as the frame.
        // Also the path when the dup is missing or the write failed.
        log::info!("system quit: SIGTERM daemon pid {pid}");
        term_spawned_group(pid);
    } else {
        log::info!("system quit: shutdown frame sent to daemon pid {pid}");
    }
    if block_until_leader_gone(pid, deadline) {
        log::warn!("daemon pid {pid} still running at the system-exit deadline; SIGKILL");
    }
    // `socket` drops here, after the leader is gone or has been killed.
}

fn reap_signalled(pid: u32, deadline: Instant) {
    if !leader_still_running(pid) {
        return;
    }
    if Instant::now() < deadline {
        log::info!("system quit: SIGTERM embeddings pid {pid}");
        term_spawned_group(pid);
    }
    if block_until_leader_gone(pid, deadline) {
        log::warn!("embeddings pid {pid} still running at the system-exit deadline; SIGKILL");
    }
}

fn write_shutdown_frame(stream: &mut TcpStream, deadline: Instant) -> bool {
    // The dup shares the tokio socket, which is non-blocking. A small
    // frame still fits; WouldBlock is the buffer, not a reason to give up
    // before the deadline. Nodelay matters only for this last write.
    let _ = stream.set_nodelay(true);
    let frame = masked_client_text_frame(SHUTDOWN_FRAME.as_bytes());
    let mut sent = 0;
    while sent < frame.len() {
        if Instant::now() >= deadline {
            return false;
        }
        match stream.write(&frame[sent..]) {
            Ok(0) => return false,
            Ok(n) => sent += n,
            Err(err) if err.kind() == std::io::ErrorKind::Interrupted => {}
            Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(err) => {
                log::warn!("system quit: shutdown frame was not written: {err}");
                return false;
            }
        }
    }
    true
}

/// Client-to-server text frame. The mask is fixed because this is
/// loopback and the only payload is the shutdown sentence.
fn masked_client_text_frame(payload: &[u8]) -> Vec<u8> {
    let mask = [0x5a, 0x3c, 0x11, 0x7e];
    let mut out = Vec::with_capacity(payload.len() + 14);
    out.push(0x81);
    if payload.len() < 126 {
        out.push(0x80 | payload.len() as u8);
    } else if payload.len() <= u16::MAX as usize {
        out.push(0x80 | 126);
        out.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    } else {
        out.push(0x80 | 127);
        out.extend_from_slice(&(payload.len() as u64).to_be_bytes());
    }
    out.extend_from_slice(&mask);
    out.extend(
        payload
            .iter()
            .enumerate()
            .map(|(i, byte)| byte ^ mask[i % 4]),
    );
    out
}

#[cfg(unix)]
fn duplicate_tcp(tcp: &tokio::net::TcpStream) -> Option<TcpStream> {
    use std::os::fd::AsFd;
    let owned = tcp.as_fd().try_clone_to_owned().ok()?;
    Some(TcpStream::from(owned))
}

#[cfg(windows)]
fn duplicate_tcp(tcp: &tokio::net::TcpStream) -> Option<TcpStream> {
    use std::os::windows::io::AsSocket;
    let owned = tcp.as_socket().try_clone_to_owned().ok()?;
    Some(TcpStream::from(owned))
}

#[cfg(not(any(unix, windows)))]
fn duplicate_tcp(tcp: &tokio::net::TcpStream) -> Option<TcpStream> {
    let _ = tcp;
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shutdown_text_is_one_masked_client_frame() {
        let frame = masked_client_text_frame(SHUTDOWN_FRAME.as_bytes());
        assert_eq!(frame[0], 0x81);
        let len = (frame[1] & 0x7f) as usize;
        assert_eq!(len, SHUTDOWN_FRAME.len());
        assert_eq!(frame[1] & 0x80, 0x80);
        let mask = &frame[2..6];
        let payload: Vec<u8> = frame[6..6 + len]
            .iter()
            .enumerate()
            .map(|(i, byte)| byte ^ mask[i % 4])
            .collect();
        assert_eq!(payload, SHUTDOWN_FRAME.as_bytes());
    }

    #[test]
    fn system_exit_budget_is_at_most_three_seconds() {
        assert!(SYSTEM_EXIT_BUDGET <= Duration::from_secs(3));
    }

    #[test]
    fn already_gone_children_do_not_wait_out_the_budget() {
        let started = Instant::now();
        reap_on_system_exit(None, None, Some(0), SYSTEM_EXIT_BUDGET);
        assert!(
            started.elapsed() < Duration::from_millis(200),
            "{:?}",
            started.elapsed()
        );
    }
}

#[cfg(all(test, unix))]
#[path = "system_exit_tests.rs"]
mod process_tests;
