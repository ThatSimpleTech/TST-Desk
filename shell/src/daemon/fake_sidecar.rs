//! Onefile-shaped stand-in for `tstd` (TD-4849). One process, its own
//! group, a websocket, then a slow exit. Unix tests only: the script
//! speaks signals and the host's group-kill is `kill` to that group.

use std::path::PathBuf;
use std::process::Stdio;
use std::time::{Duration, Instant};

use super::ClientWs;

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

pub(crate) struct Fake {
    dir: PathBuf,
    pub(crate) child: tokio::process::Child,
    pub(crate) marker: PathBuf,
    pub(crate) how: PathBuf,
    err: PathBuf,
    port: u16,
}

impl Fake {
    pub(crate) async fn spawn(mode: &str) -> Self {
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

    pub(crate) async fn connect(&self) -> ClientWs {
        let (ws, _) = tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{}", self.port))
            .await
            .expect("connect to fake sidecar");
        ws
    }

    pub(crate) fn stderr_text(&self) -> String {
        std::fs::read_to_string(&self.err).unwrap_or_default()
    }

    pub(crate) fn pid(&self) -> u32 {
        self.child.id().expect("fake sidecar pid")
    }
}

impl Drop for Fake {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn scratch() -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    // `SystemTime` on macOS is coarser than a nanosecond, and several
    // tests spawn this helper on the same tick. `create_dir_all` would
    // then share one directory and one port file across processes.
    let n = N.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!(
        "tstd-shutdown-{}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
        n
    ));
    std::fs::create_dir(&dir).unwrap();
    dir
}

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
