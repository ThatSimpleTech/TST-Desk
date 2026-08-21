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
use tokio_tungstenite::tungstenite::Message;
use tst_desk_lib::daemon::{
    connect_handshake, kill_spawned_group, live_port_file, parent_pid_argv,
    port_file_belongs_to_spawn, should_spawn_new_daemon, spawn_daemon, wait_for_port_file,
};

fn make_dir() -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "tstd-supervision-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
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

    assert!(
        port_file_belongs_to_spawn(pid, pf.pid),
        "port file pid {} must be spawned {} or a descendant",
        pf.pid,
        pid
    );

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

/// A live leftover `tstd` is attached, not spawned again (TD-2902).
#[tokio::test]
async fn attaches_to_live_port_file_without_second_spawn() {
    let dir = make_dir();
    std::fs::write(dir.join("coworker.yaml"), "enabled: true\n").expect("coworker on");
    let mut child = spawn_daemon(&dir).expect("spawn tstd");
    let pid = child.id().expect("daemon handed over its pid");

    let pf = wait_for_port_file(&dir, pid, Duration::from_secs(30))
        .await
        .expect("daemon should write a port file");

    let live = live_port_file(&dir).expect("port.json must name a live listener");
    assert_eq!(live.port, pf.port);
    assert!(!should_spawn_new_daemon(live.pid, true));
    assert!(parent_pid_argv(true, 1).is_empty());

    let mut ws = connect_handshake(live.port, &live.token)
        .await
        .expect("attach handshake against the first daemon");
    ws.send(Message::Text(r#"{"type":"shutdown"}"#.into()))
        .await
        .expect("send shutdown");
    drop(ws);

    tokio::time::timeout(Duration::from_secs(10), child.wait())
        .await
        .expect("daemon should exit promptly after shutdown")
        .expect("wait on child");
    cleanup(&dir);
}

/// The packaged sidecar is a bootloader that stays alive and spawns the
/// real daemon as a child (TD-1304). A shell wrapper that *doesn't* exec
/// is that shape: the host's child pid is the wrapper, the port file names
/// the grandchild. Exact-pid matching would time out; descendant matching
/// must attach, and group-kill must reap both.
#[cfg(unix)]
#[tokio::test]
async fn onefile_shape_attaches_and_group_kill_reaps_grandchild() {
    use std::os::unix::fs::PermissionsExt;

    let core = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("repo root")
        .join("core");
    let tstd = core.join(".venv").join("bin").join("tstd");
    assert!(
        tstd.exists(),
        "dev tstd missing at {}; run `uv sync` in core/",
        tstd.display()
    );

    let dir = make_dir();
    let wrapper = dir.join("tstd-wrapper");
    // No `exec`: the wrapper stays the host's child, tstd is the grandchild.
    std::fs::write(&wrapper, format!("#!/bin/sh\n{} \"$@\"\n", tstd.display()))
        .expect("write wrapper");
    std::fs::set_permissions(&wrapper, std::fs::Permissions::from_mode(0o755))
        .expect("chmod wrapper");

    let previous = std::env::var_os("TSTD_PATH");
    std::env::set_var("TSTD_PATH", &wrapper);
    let mut child = spawn_daemon(&dir).expect("spawn wrapper");
    match previous {
        Some(v) => std::env::set_var("TSTD_PATH", v),
        None => std::env::remove_var("TSTD_PATH"),
    }
    let spawned = child.id().expect("wrapper handed over its pid");

    let pf = wait_for_port_file(&dir, spawned, Duration::from_secs(30))
        .await
        .expect("descendant port file must be accepted");
    assert_ne!(
        pf.pid, spawned,
        "wrapper must stay parent so the port file names a grandchild"
    );
    assert!(
        port_file_belongs_to_spawn(spawned, pf.pid),
        "grandchild {} must belong to wrapper {}",
        pf.pid,
        spawned
    );

    // Handshake proves the listener is the one we accepted.
    let ws = connect_handshake(pf.port, &pf.token)
        .await
        .expect("hello handshake against the grandchild");
    drop(ws);

    kill_spawned_group(spawned);
    let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;

    let grandchild_alive = std::process::Command::new("kill")
        .args(["-0", &pf.pid.to_string()])
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    assert!(
        !grandchild_alive,
        "group kill must reap the grandchild (pid {})",
        pf.pid
    );

    cleanup(&dir);
}
