//! Process-level checks for quit (TD-4849). The fake sidecar is the
//! onefile shape the host actually waits on: one process, its own
//! group, a websocket, then a slow exit.

use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
#[cfg(unix)]
use std::process::Stdio;
use tokio::net::TcpListener;

#[cfg(unix)]
use super::super::fake_sidecar::Fake;
#[cfg(unix)]
use super::{block_until_leader_gone, shutdown_and_reap, Reap, SHUTDOWN_GRACE};
use super::{hold_until_peer_closes, read_until_closed, SHUTDOWN_FRAME};

#[cfg(unix)]
#[tokio::test]
async fn host_waits_for_a_slow_shutdown_and_does_not_sigkill() {
    let mut fake = Fake::spawn("obey").await;
    let pid = fake.pid();
    let ws = fake.connect().await;
    let started = Instant::now();
    let reap = tokio::time::timeout(
        SHUTDOWN_GRACE + Duration::from_secs(3),
        shutdown_and_reap(Some(ws), &mut fake.child, pid, SHUTDOWN_GRACE),
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
        shutdown_and_reap(Some(ws), &mut fake.child, pid, grace),
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

/// The server's keepalive closed an unread supervision socket after ~40 s,
/// so Quit's frame reached nobody. The reader must answer every ping and
/// return once the peer closes.
#[tokio::test]
async fn supervision_reader_answers_pings_until_close() {
    use tokio_tungstenite::tungstenite::Message;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut peer = tokio_tungstenite::accept_async(stream).await.unwrap();
        let mut pongs = Vec::new();
        for key in ["k1", "k2"] {
            peer.send(Message::Text(r#"{"type":"ping"}"#.into()))
                .await
                .unwrap();
            peer.send(Message::Ping(key.as_bytes().to_vec().into()))
                .await
                .unwrap();
            let pong = tokio::time::timeout(Duration::from_secs(2), async {
                loop {
                    match peer.next().await {
                        Some(Ok(Message::Pong(data))) => break Some(data.to_vec()),
                        Some(Ok(_)) => {}
                        _ => break None,
                    }
                }
            })
            .await
            .ok()
            .flatten();
            pongs.push(pong);
        }
        let _ = peer.close(None).await;
        pongs
    });

    let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(6), read_until_closed(&mut ws))
        .await
        .expect("reader did not return after the peer closed");
    let pongs = server.await.unwrap();
    assert_eq!(
        pongs,
        vec![Some(b"k1".to_vec()), Some(b"k2".to_vec())],
        "a keepalive ping went unanswered"
    );
}

#[cfg(unix)]
#[tokio::test]
async fn quit_on_a_closed_socket_asks_with_sigterm() {
    let mut fake = Fake::spawn("obey").await;
    let pid = fake.pid();
    let started = Instant::now();
    let reap = tokio::time::timeout(
        SHUTDOWN_GRACE + Duration::from_secs(3),
        shutdown_and_reap(None, &mut fake.child, pid, SHUTDOWN_GRACE),
    )
    .await
    .expect("shutdown wait hung");
    let elapsed = started.elapsed();
    assert!(
        matches!(reap, Reap::Exited { code: Some(0) }),
        "reap={reap:?} elapsed={elapsed:?} stderr={}",
        fake.stderr_text()
    );
    assert_eq!(std::fs::read_to_string(&fake.how).unwrap(), "term");
    assert!(
        elapsed < SHUTDOWN_GRACE,
        "waited out the whole grace: {elapsed:?}"
    );
}
