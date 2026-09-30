//! Process-level checks for quit (TD-4849). The fake sidecar is the
//! onefile shape the host actually waits on: one process, its own
//! group, a websocket, then a slow exit.

use std::path::PathBuf;
use std::time::{Duration, Instant};

use futures_util::StreamExt;
#[cfg(unix)]
use std::process::Stdio;
use tokio::net::TcpListener;

#[cfg(unix)]
use super::{block_until_leader_gone, shutdown_and_reap, Reap, SHUTDOWN_GRACE};
use super::{hold_until_peer_closes, SHUTDOWN_FRAME};

#[cfg(unix)]
fn scratch() -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "tstd-shutdown-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[cfg(unix)]
const FAKE_SIDECAR: &str = r#"
import base64, hashlib, os, signal, socket, struct, sys, time
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
marker, mode, ready, how = sys.argv[1:5]
PAUSE = 1.5

def finish(kind):
    time.sleep(PAUSE)
    with open(how, "w", encoding="utf-8") as fh:
        fh.write(kind)
    try:
        os.remove(marker)
    except FileNotFoundError:
        pass
    os._exit(0)

if mode == "obey":
    signal.signal(signal.SIGTERM, lambda *_: finish("term"))
else:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

def recvn(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise SystemExit(2)
        buf += chunk
    return buf

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
with open(ready, "w", encoding="utf-8") as fh:
    fh.write(str(srv.getsockname()[1]))
    fh.flush()
conn, _ = srv.accept()
data = b""
while b"\r\n\r\n" not in data:
    chunk = conn.recv(4096)
    if not chunk:
        raise SystemExit(2)
    data += chunk
key = ""
for line in data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1").split("\r\n"):
    if line.lower().startswith("sec-websocket-key:"):
        key = line.split(":", 1)[1].strip()
accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
conn.sendall(
    (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode("ascii")
)
hdr = recvn(conn, 2)
length = hdr[1] & 0x7F
if length == 126:
    length = struct.unpack("!H", recvn(conn, 2))[0]
elif length == 127:
    length = struct.unpack("!Q", recvn(conn, 8))[0]
mask = recvn(conn, 4) if hdr[1] & 0x80 else b""
payload = recvn(conn, length)
if mask:
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
if mode == "ignore" or b"shutdown" not in payload:
    time.sleep(60)
    raise SystemExit(3)
conn.sendall(bytes([0x88, 0x00]))
conn.close()
finish("frame")
"#;

#[cfg(unix)]
struct Fake {
    dir: PathBuf,
    child: tokio::process::Child,
    marker: PathBuf,
    how: PathBuf,
    err: PathBuf,
    port: u16,
}

#[cfg(unix)]
impl Fake {
    async fn spawn(mode: &str) -> Self {
        let dir = scratch();
        let script = dir.join("fake_tstd.py");
        let marker = dir.join("marker");
        let ready = dir.join("ready");
        let how = dir.join("how");
        let err = dir.join("err");
        std::fs::write(&script, FAKE_SIDECAR).unwrap();
        std::fs::write(&marker, "keep\n").unwrap();
        let err_file = std::fs::File::create(&err).unwrap();
        let child = tokio::process::Command::new("python3")
            .arg(&script)
            .arg(&marker)
            .arg(mode)
            .arg(&ready)
            .arg(&how)
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(err_file)
            .kill_on_drop(true)
            .spawn()
            .expect("python3");
        let port = read_port(&ready, &err).await;
        Self {
            dir,
            child,
            marker,
            how,
            err,
            port,
        }
    }

    fn stderr_text(&self) -> String {
        std::fs::read_to_string(&self.err).unwrap_or_default()
    }

    fn pid(&self) -> u32 {
        self.child.id().expect("fake sidecar pid")
    }
}

#[cfg(unix)]
impl Drop for Fake {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

#[cfg(unix)]
async fn read_port(path: &std::path::Path, err: &std::path::Path) -> u16 {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Ok(text) = std::fs::read_to_string(path) {
            if let Ok(port) = text.trim().parse::<u16>() {
                return port;
            }
        }
        if Instant::now() >= deadline {
            let stderr = std::fs::read_to_string(err).unwrap_or_default();
            panic!("fake sidecar never wrote a port\n{stderr}");
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

#[cfg(unix)]
async fn connect_fake(port: u16) -> super::super::ClientWs {
    let (ws, _) = tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{port}"))
        .await
        .expect("connect to fake sidecar");
    ws
}

#[cfg(unix)]
#[tokio::test]
async fn host_waits_for_a_slow_shutdown_and_does_not_sigkill() {
    let mut fake = Fake::spawn("obey").await;
    let pid = fake.pid();
    let ws = connect_fake(fake.port).await;
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
    let ws = connect_fake(fake.port).await;
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
