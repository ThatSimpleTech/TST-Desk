//! Embeddings sidecar supervision (TD-2204).
//!
//! Mirrors `daemon_supervision.rs`: spawn through the host helpers, reap
//! the process group on quit, and prove a dead sidecar does not take
//! `tstd` with it. Empty / `base_url`-only config never calls spawn.

use std::time::Duration;

use futures_util::SinkExt;
use tokio_tungstenite::tungstenite::Message;
use tst_desk_lib::daemon::embeddings::{
    embeddings_argv_from_yaml, load_embeddings_argv, run_supervision, spawn_embeddings,
    yaml_is_attach_only, EmbeddingsHandle,
};
use tst_desk_lib::daemon::{
    connect_handshake, kill_spawned_group, spawn_daemon, wait_for_port_file,
};

fn make_dir(tag: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("tstd-embeddings-{}-{}", tag, std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create data dir");
    dir
}

fn cleanup(dir: &std::path::Path) {
    let _ = std::fs::remove_dir_all(dir);
}

fn write_config(dir: &std::path::Path, body: &str) {
    std::fs::write(dir.join("config.yaml"), body).expect("write config.yaml");
}

async fn wait_for_child_pid(h: &EmbeddingsHandle, timeout: Duration) -> Option<u32> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if let Some(pid) = h.child_pid() {
            return Some(pid);
        }
        if tokio::time::Instant::now() >= deadline {
            return None;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

fn pid_alive(pid: u32) -> bool {
    std::process::Command::new("kill")
        .args(["-0", &pid.to_string()])
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[test]
fn packaged_shape_is_attach_only() {
    // Shipped config fills base_url and never names a command.
    let text = concat!(
        "embeddings:\n",
        "  base_url: http://127.0.0.1:8080/v1\n",
        "  model: nomic-embed-text\n",
        "  timeout_seconds: 2\n",
        "  top_k: 4\n",
        "  token_budget: 2000\n",
    );
    assert!(yaml_is_attach_only(text));
    assert_eq!(embeddings_argv_from_yaml(text), None);
}

#[test]
fn missing_config_never_resolves_a_command() {
    let dir = make_dir("missing");
    assert_eq!(load_embeddings_argv(&dir.join("config.yaml")), None);
    cleanup(&dir);
}

#[tokio::test]
async fn empty_command_never_spawns_a_child() {
    let dir = make_dir("empty");
    write_config(&dir, "embeddings:\n  base_url: http://127.0.0.1:8080/v1\n");
    let h = EmbeddingsHandle::new(dir.clone());
    let task = tokio::spawn({
        let h = h.clone();
        async move { run_supervision(h).await }
    });
    tokio::time::timeout(Duration::from_secs(2), h.wait_for_done())
        .await
        .expect("attach-only supervisor finishes");
    assert!(h.child_pid().is_none(), "empty command must not spawn");
    let _ = task.await;
    cleanup(&dir);
}

#[cfg(unix)]
#[tokio::test]
async fn configured_command_spawns_and_quit_reaps() {
    let dir = make_dir("reap");
    write_config(&dir, "embeddings:\n  command: [/bin/sleep, \"60\"]\n");
    let h = EmbeddingsHandle::new(dir.clone());
    let task = tokio::spawn({
        let h = h.clone();
        async move { run_supervision(h).await }
    });

    let pid = wait_for_child_pid(&h, Duration::from_secs(2))
        .await
        .expect("configured command must spawn");
    assert!(pid_alive(pid), "sidecar should be running before quit");

    h.request_shutdown();
    tokio::time::timeout(Duration::from_secs(5), h.wait_for_done())
        .await
        .expect("quit should reap promptly");
    let _ = task.await;

    assert!(!pid_alive(pid), "quit must reap the embeddings child");
    cleanup(&dir);
}

#[cfg(unix)]
#[tokio::test]
async fn spawn_helper_reaps_via_process_group() {
    let mut child = spawn_embeddings(&["/bin/sleep".into(), "60".into()]).expect("spawn sleep");
    let pid = child.id().expect("pid");
    kill_spawned_group(pid);
    tokio::time::timeout(Duration::from_secs(5), child.wait())
        .await
        .expect("group kill should reap the child")
        .expect("wait");
    assert!(!pid_alive(pid));
}

/// A dead sidecar must not take `tstd` down (heading-match is the floor).
#[cfg(unix)]
#[tokio::test]
async fn dead_sidecar_leaves_daemon_running() {
    let dir = make_dir("iso");
    let mut daemon = spawn_daemon(&dir).expect("spawn tstd");
    let daemon_pid = daemon.id().expect("daemon pid");
    let pf = wait_for_port_file(&dir, daemon_pid, Duration::from_secs(30))
        .await
        .expect("daemon should write a port file");

    let mut sidecar = spawn_embeddings(&["/bin/sleep".into(), "60".into()]).expect("spawn sidecar");
    let sidecar_pid = sidecar.id().expect("sidecar pid");
    kill_spawned_group(sidecar_pid);
    let _ = tokio::time::timeout(Duration::from_secs(5), sidecar.wait()).await;
    assert!(!pid_alive(sidecar_pid), "sidecar should be dead");

    let mut ws = connect_handshake(pf.port, &pf.token)
        .await
        .expect("daemon must survive sidecar death");
    ws.send(Message::Text(r#"{"type":"shutdown"}"#.into()))
        .await
        .expect("send shutdown");
    drop(ws);
    tokio::time::timeout(Duration::from_secs(10), daemon.wait())
        .await
        .expect("daemon should exit after its own shutdown")
        .expect("wait on daemon");

    cleanup(&dir);
}

/// Wrapper shape: host child stays up, grandchild is the payload.
/// Group-kill on quit must reap both, same as the tstd onefile test.
#[cfg(unix)]
#[tokio::test]
async fn quit_reaps_wrapper_and_grandchild() {
    use std::os::unix::fs::PermissionsExt;

    let dir = make_dir("wrap");
    let wrapper = dir.join("embed-wrapper");
    std::fs::write(&wrapper, "#!/bin/sh\nexec /bin/sleep 60\n").expect("write wrapper");
    std::fs::set_permissions(&wrapper, std::fs::Permissions::from_mode(0o755))
        .expect("chmod wrapper");

    write_config(
        &dir,
        &format!("embeddings:\n  command: [\"{}\"]\n", wrapper.display()),
    );
    let h = EmbeddingsHandle::new(dir.clone());
    let task = tokio::spawn({
        let h = h.clone();
        async move { run_supervision(h).await }
    });

    let spawned = wait_for_child_pid(&h, Duration::from_secs(2))
        .await
        .expect("wrapper must spawn");

    h.request_shutdown();
    tokio::time::timeout(Duration::from_secs(5), h.wait_for_done())
        .await
        .expect("quit should reap the wrapper tree");
    let _ = task.await;
    assert!(!pid_alive(spawned), "group kill must reap the wrapper");
    cleanup(&dir);
}
