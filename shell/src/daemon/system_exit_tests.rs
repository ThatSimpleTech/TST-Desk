//! `RunEvent::Exit` with no `ExitRequested` (TD-4850). The fake sidecar
//! is the TD-4849 one: it removes a marker 1.5s after a shutdown frame
//! or SIGTERM, and ignores both when started in `ignore` mode.

use std::process::Stdio;
use std::time::{Duration, Instant};

use super::super::fake_sidecar::Fake;
use super::{duplicate_client_socket, reap_on_system_exit, SYSTEM_EXIT_BUDGET};

fn within_one_budget(elapsed: Duration, budget: Duration) -> bool {
    elapsed < budget + budget / 2
}

#[tokio::test]
async fn system_exit_waits_for_a_slow_sidecar_and_does_not_sigkill() {
    let mut fake = Fake::spawn("obey").await;
    let pid = fake.pid();
    let ws = fake.connect().await;
    let socket = duplicate_client_socket(&ws).expect("dup of the live socket");
    let started = Instant::now();
    reap_on_system_exit(Some(socket), Some(pid), None, SYSTEM_EXIT_BUDGET);
    let elapsed = started.elapsed();
    let status = tokio::time::timeout(Duration::from_secs(2), fake.child.wait())
        .await
        .expect("leader was not reaped")
        .unwrap();
    assert!(
        status.success(),
        "status={status} elapsed={elapsed:?} stderr={}",
        fake.stderr_text()
    );
    assert!(!fake.marker.exists(), "marker still present");
    assert_eq!(std::fs::read_to_string(&fake.how).unwrap(), "frame");
    assert!(
        elapsed >= Duration::from_millis(1200),
        "returned too fast to have waited: {elapsed:?}"
    );
    assert!(
        elapsed < SYSTEM_EXIT_BUDGET,
        "used the whole budget on a cooperative sidecar: {elapsed:?}"
    );
    drop(ws);
}

#[tokio::test]
async fn system_exit_sigterms_when_the_socket_is_unavailable() {
    let mut fake = Fake::spawn("obey").await;
    let pid = fake.pid();
    // Connected, so the fake is blocked in the frame read, but the exit
    // path is not given that socket. SIGTERM is the fallback.
    let ws = fake.connect().await;
    let started = Instant::now();
    reap_on_system_exit(None, Some(pid), None, SYSTEM_EXIT_BUDGET);
    let elapsed = started.elapsed();
    let status = tokio::time::timeout(Duration::from_secs(2), fake.child.wait())
        .await
        .expect("leader was not reaped")
        .unwrap();
    assert!(
        status.success(),
        "status={status} elapsed={elapsed:?} stderr={}",
        fake.stderr_text()
    );
    assert!(!fake.marker.exists(), "SIGTERM left the marker");
    assert_eq!(std::fs::read_to_string(&fake.how).unwrap(), "term");
    assert!(elapsed < SYSTEM_EXIT_BUDGET, "{elapsed:?}");
    drop(ws);
}

#[tokio::test]
async fn system_exit_sigkills_an_unresponsive_sidecar_at_the_bound() {
    let mut fake = Fake::spawn("ignore").await;
    let pid = fake.pid();
    let ws = fake.connect().await;
    let socket = duplicate_client_socket(&ws).expect("dup of the live socket");
    let budget = Duration::from_millis(700);
    let started = Instant::now();
    reap_on_system_exit(Some(socket), Some(pid), None, budget);
    let elapsed = started.elapsed();
    let status = tokio::time::timeout(Duration::from_secs(2), fake.child.wait())
        .await
        .expect("leader was not reaped")
        .unwrap();
    assert!(
        !status.success(),
        "unresponsive sidecar exited cleanly: {status} stderr={}",
        fake.stderr_text()
    );
    assert!(fake.marker.exists(), "SIGKILL still removed the marker");
    assert!(!fake.how.exists(), "SIGKILL recorded a clean finish");
    assert!(
        elapsed >= budget,
        "killed before the bound: {elapsed:?} bound={budget:?}"
    );
    assert!(
        within_one_budget(elapsed, budget),
        "wait was not one budget: {elapsed:?} budget={budget:?}"
    );
    assert!(!crate::daemon::pid_is_alive(pid));
    drop(ws);
}

#[tokio::test]
async fn dead_embeddings_does_not_extend_the_budget() {
    let mut gone = tokio::process::Command::new("python3")
        .args(["-c", "import sys; sys.exit(0)"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("python3");
    let gone_pid = gone.id().expect("pid");
    tokio::time::timeout(Duration::from_secs(2), gone.wait())
        .await
        .expect("short process hung")
        .unwrap();

    let mut fake = Fake::spawn("ignore").await;
    let pid = fake.pid();
    let ws = fake.connect().await;
    let socket = duplicate_client_socket(&ws).expect("dup of the live socket");
    let budget = Duration::from_millis(700);
    let started = Instant::now();
    reap_on_system_exit(Some(socket), Some(pid), Some(gone_pid), budget);
    let elapsed = started.elapsed();
    let _ = tokio::time::timeout(Duration::from_secs(2), fake.child.wait()).await;
    assert!(elapsed >= budget, "killed before the bound: {elapsed:?}");
    assert!(
        within_one_budget(elapsed, budget),
        "a dead embeddings pid added a second wait: {elapsed:?} budget={budget:?}"
    );
    assert!(!crate::daemon::pid_is_alive(gone_pid));
    drop(ws);
}

#[tokio::test]
async fn daemon_and_embeddings_share_one_budget() {
    let mut embed = tokio::process::Command::new("python3")
        .args([
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ])
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .expect("python3");
    let embed_pid = embed.id().expect("embeddings pid");

    let mut fake = Fake::spawn("ignore").await;
    let pid = fake.pid();
    let ws = fake.connect().await;
    let socket = duplicate_client_socket(&ws).expect("dup of the live socket");
    let budget = Duration::from_millis(700);
    let started = Instant::now();
    reap_on_system_exit(Some(socket), Some(pid), Some(embed_pid), budget);
    let elapsed = started.elapsed();
    let _ = tokio::time::timeout(Duration::from_secs(2), fake.child.wait()).await;
    let embed_status = tokio::time::timeout(Duration::from_secs(2), embed.wait())
        .await
        .expect("embeddings sidecar was not reaped")
        .unwrap();
    assert!(
        !embed_status.success(),
        "embeddings sidecar exited cleanly: {embed_status}"
    );
    assert!(elapsed >= budget, "killed before the bound: {elapsed:?}");
    assert!(
        within_one_budget(elapsed, budget),
        "daemon and embeddings each took a budget: {elapsed:?} budget={budget:?}"
    );
    assert!(!crate::daemon::pid_is_alive(pid));
    assert!(!crate::daemon::pid_is_alive(embed_pid));
    drop(ws);
}
