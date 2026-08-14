//! End-to-end supervision test (TD-1002, criterion 1).
//!
//! Spawns the real `tstd` daemon through the host's own functions, discovers
//! it via the port file, completes the WebSocket `hello` handshake, then
//! shuts it down cleanly and checks the port file is removed.
//!
//! Running this requires the `core` package to be runnable via uv (the debug
//! `resolve_command` fallback spawns `uv run --directory ../core tstd`).

use std::time::Duration;

use futures_util::SinkExt;
use tst_desk_lib::daemon::{connect_handshake, spawn_daemon, wait_for_port_file};
use tokio_tungstenite::tungstenite::Message;

fn make_dir() -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("tstd-supervision-{}", std::process::id()));
    std::fs::create_dir_all(&dir).expect("create data dir");
    dir
}

fn cleanup(dir: &std::path::Path) {
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn spawns_daemon_connects_via_port_file_and_shuts_down_cleanly() {
    let dir = make_dir();
    let mut child = spawn_daemon(&dir).expect("spawn tstd");
    let pid = child.id().expect("daemon handed over its pid");

    let pf = wait_for_port_file(&dir, pid, Duration::from_secs(30))
        .await
        .expect("daemon should write a port file");

    assert_eq!(pf.pid, pid, "port file must name the child we spawned");

    let mut ws = connect_handshake(pf.port, &pf.token)
        .await
        .expect("hello handshake should succeed");

    // Clean shutdown: send `shutdown`, the daemon exits 0 and removes the file.
    // No WS close-ack await — the daemon tears down the connection itself.
    ws.send(Message::Text(r#"{"type":"shutdown"}"#.into()))
        .await
        .expect("send shutdown");
    drop(ws);

    tokio::time::timeout(Duration::from_secs(10), child.wait())
        .await
        .expect("daemon should exit promptly after shutdown")
        .expect("wait on child");

    let after = tokio::fs::try_exists(dir.join("port.json"))
        .await
        .expect("stat port file");
    assert!(!after, "clean shutdown must remove the port file");
    cleanup(&dir);
}