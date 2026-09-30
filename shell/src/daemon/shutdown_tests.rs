//! Process-level checks for quit (TD-4849). The fake sidecar is the
//! onefile shape the host actually waits on: one process, its own
//! group, a websocket, then a slow exit.

use std::time::{Duration, Instant};

use futures_util::StreamExt;
#[cfg(unix)]
use std::process::Stdio;
use tokio::net::TcpListener;

#[cfg(unix)]
use super::super::fake_sidecar::Fake;
#[cfg(unix)]
use super::{block_until_leader_gone, shutdown_and_reap, Reap, SHUTDOWN_GRACE};
use super::{hold_until_peer_closes, SHUTDOWN_FRAME};

#[cfg(unix)]
#[tokio::test]
async fn host_waits_for_a_slow_shutdown_and_does_not_sigkill() {
    let mut fake = Fake::spawn("obey").await;
    let pid = fake.pid();
    let ws = fake.connect().await;
    let started = Instant::now();
    let reap = tokio::time::timeout(
        SHUTDOWN_GRACE + Duration::from_secs(3),
        shutdown_and_reap(ws, &mut fake.child, pid, SHUTDOWN_GRACE),
    )
    .await
    .expect("shutdown wait hung");
    let elapsed = started.elapsed();
    assert!(
        matches!(reap, Reap::Exited { code: Some(0) }),
        "reap={reap:?} elapsed={elapsed:?} stderr={}",
        fake.stderr_text()
    );
    assert!(
        !fake.marker.exists(),
        "marker still present after clean exit"
    );
    assert_eq!(std::fs::read_to_string(&fake.how).unwrap(), "frame");
    // The script sleeps 1.5s after it has the frame. A SIGKILL inside
    // the grace would leave the marker and a non-zero wait status.
    assert!(
        elapsed >= Duration::from_millis(1200),
        "returned too fast to have waited: {elapsed:?}"
    );
    assert!(
        elapsed < SHUTDOWN_GRACE,
        "waited out the whole grace: {elapsed:?}"
    );
    assert_ne!(pid, std::process::id());
}

#[cfg(unix)]
#[tokio::test]
async fn host_sigkills_when_shutdown_is_ignored_past_the_bound() {
    let mut fake = Fake::spawn("ignore").await;
    let pid = fake.pid();
    let ws = fake.connect().await;
    let grace = Duration::from_millis(800);
    let started = Instant::now();
    let reap = tokio::time::timeout(
        Duration::from_secs(5),
        shutdown_and_reap(ws, &mut fake.child, pid, grace),
    )
    .await
    .expect("shutdown wait hung");
    let elapsed = started.elapsed();
    assert!(
        matches!(reap, Reap::Killed),
        "reap={reap:?} elapsed={elapsed:?} stderr={}",
        fake.stderr_text()
    );
    assert!(fake.marker.exists(), "ignored shutdown removed the marker");
    assert!(
        !fake.how.exists(),
        "ignored shutdown recorded a clean finish"
    );
    assert!(
        elapsed >= grace,
        "killed before the bound: {elapsed:?} bound={grace:?}"
    );
    assert!(
        elapsed < Duration::from_secs(4),
        "did not kill when the bound expired: {elapsed:?}"
    );
    assert!(!crate::daemon::pid_is_alive(pid));
}

#[cfg(unix)]
#[tokio::test]
async fn exit_backstop_does_not_kill_inside_the_grace() {
    let mut child = tokio::process::Command::new("python3")
        .args(["-c", "import time; time.sleep(0.4)"])
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .expect("python3");
    let pid = child.id().unwrap();
    let started = Instant::now();
    block_until_leader_gone(pid, Instant::now() + Duration::from_secs(3));
    let status = tokio::time::timeout(Duration::from_secs(2), child.wait())
        .await
        .expect("leader wait hung")
        .unwrap();
    let elapsed = started.elapsed();
    assert!(status.success(), "{status}");
    assert!(elapsed >= Duration::from_millis(300), "{elapsed:?}");
    assert!(elapsed < Duration::from_secs(2), "{elapsed:?}");
}

#[cfg(unix)]
#[tokio::test]
async fn exit_backstop_kills_only_after_the_grace() {
    let mut child = tokio::process::Command::new("python3")
        .args([
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ])
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .expect("python3");
    let pid = child.id().unwrap();
    let grace = Duration::from_millis(500);
    let started = Instant::now();
    block_until_leader_gone(pid, Instant::now() + grace);
    let status = tokio::time::timeout(Duration::from_secs(2), child.wait())
        .await
        .expect("leader was not reaped")
        .unwrap();
    let elapsed = started.elapsed();
    assert!(
        !status.success(),
        "SIGTERM-ignoring process exited cleanly: {status}"
    );
    assert!(elapsed >= Duration::from_millis(400), "{elapsed:?}");
    assert!(elapsed < Duration::from_secs(2), "{elapsed:?}");
    assert!(!crate::daemon::pid_is_alive(pid));
}

#[tokio::test]
async fn shutdown_frame_is_held_until_the_peer_closes() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let release = std::sync::Arc::new(tokio::sync::Notify::new());
    let release_bg = release.clone();
    let got = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let got_bg = got.clone();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut peer = tokio_tungstenite::accept_async(stream).await.unwrap();
        let first = peer.next().await;
        let saw_shutdown = matches!(
            &first,
            Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text)))
                if text.as_str() == SHUTDOWN_FRAME
        );
        // Register before publishing `got`, so notify_one cannot land
        // in the gap where no waiter exists (it would not be stored).
        let wait = release_bg.notified();
        tokio::pin!(wait);
        wait.as_mut().enable();
        if saw_shutdown {
            got_bg.store(true, std::sync::atomic::Ordering::SeqCst);
        }
        wait.await;
        let _ = peer.close(None).await;
        first
    });

    let (ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
        .await
        .unwrap();
    let host = tokio::spawn(async move {
        hold_until_peer_closes(ws, Instant::now() + Duration::from_secs(3)).await;
    });

    let saw_frame = tokio::time::timeout(Duration::from_secs(2), async {
        while !got.load(std::sync::atomic::Ordering::SeqCst) {
            tokio::task::yield_now().await;
        }
    })
    .await;
    assert!(saw_frame.is_ok(), "peer never saw the shutdown frame");
    assert!(
        !host.is_finished(),
        "host dropped the socket before the peer closed"
    );
    release.notify_one();
    tokio::time::timeout(Duration::from_secs(2), host)
        .await
        .expect("host did not return after the peer closed")
        .unwrap();
    match server.await.unwrap() {
        Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text))) => {
            assert_eq!(text.as_str(), SHUTDOWN_FRAME);
        }
        other => panic!("expected the shutdown frame, got {other:?}"),
    }
}
