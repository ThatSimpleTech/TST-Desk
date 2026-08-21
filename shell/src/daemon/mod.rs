//! Daemon supervision (TD-1002).
//!
//! The Tauri host spawns the `tstd` Python daemon on launch, discovers it
//! through its port file (`{port, token, pid}`), completes a WebSocket
//! `hello` handshake, watches for crash, and restarts it with a bounded
//! backoff. The matching side of each contract lives in `core/tstd`:
//!
//! - port file: `core/tstd/ws.py` (`create_port_file_path`, `write_port_file`)
//! - handshake + shutdown messages: `core/tstd/protocol.py`
//! - orphan watchdog: `core/tstd/daemon.py` (`--parent-pid`, coworker off)
//! - coworker flag: `{data_dir}/coworker.yaml` (TD-2902)
//!
//! Quit still best-effort-kills from `RunEvent::Exit`. Close hides the
//! window and leaves `tstd` running when coworker mode is on; spawn then
//! omits `--parent-pid` so a dead window process does not reap the daemon.
//!
//! PyInstaller's `--onefile` sidecar (TD-1301) is a bootloader that spawns
//! the real daemon as a child. Port-file matching and group-kill live in
//! [`daemon_pid`] (TD-1304).

pub mod coworker;
mod daemon_pid;
pub mod embeddings;

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use tauri::{AppHandle, Emitter};
use tokio::sync::{watch, Notify};
use tokio_tungstenite::tungstenite::Message;

pub use coworker::{
    load_coworker, parent_pid_argv, should_spawn_new_daemon, window_close_action, CloseAction,
    LifecycleEvent,
};
pub use daemon_pid::{kill_spawned_group, pid_is_alive, port_file_belongs_to_spawn};

/// Protocol version advertised in the `hello` handshake.
pub const PROTOCOL_VERSION: u32 = 1;
/// Hard cap on automatic restarts after crashes before giving up.
pub const MAX_RESTARTS: u32 = 3;
/// How long the host waits for a fresh port file after spawning.
const PORT_FILE_TIMEOUT: Duration = Duration::from_secs(30);
/// How long the host waits for a `hello_ack` before declaring the daemon bad.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
/// How long the host waits after sending `shutdown` before sending SIGKILL.
const SHUTDOWN_GRACE: Duration = Duration::from_secs(5);

type ClientWs =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// Live connection details the rest of the app can read (TD-1003).
#[derive(Debug, Clone)]
pub struct ConnInfo {
    pub port: u16,
    pub token: String,
}

/// High-level lifecycle state, surfaced to the UI as `daemon-status`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DaemonStatus {
    Starting,
    Connected { port: u16 },
    Crashed { restart: u32 },
    Stopping,
    Stopped,
}

impl DaemonStatus {
    fn state_str(self) -> &'static str {
        match self {
            Self::Starting => "starting",
            Self::Connected { .. } => "connected",
            Self::Crashed { .. } => "crashed",
            Self::Stopping => "stopping",
            Self::Stopped => "stopped",
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct PortFile {
    pub port: u16,
    pub token: String,
    #[serde(default)]
    pub pid: u32,
}

/// Handle the rest of the app uses to read status and request shutdown.
/// Every field is copyable/`Clone`, and the supervision task is the owner.
#[derive(Clone)]
pub struct DaemonHandle {
    app: AppHandle,
    data_dir: PathBuf,
    status_tx: watch::Sender<DaemonStatus>,
    status_rx: watch::Receiver<DaemonStatus>,
    shutdown: Arc<AtomicBool>,
    shutdown_notify: Arc<Notify>,
    conn: Arc<Mutex<Option<ConnInfo>>>,
    child_pid: Arc<AtomicI32>,
    done: Arc<Notify>,
}

impl DaemonHandle {
    /// Current status (last value of the watch channel).
    pub fn status(&self) -> DaemonStatus {
        *self.status_rx.borrow()
    }

    /// Latest live connection info, if the daemon is currently up.
    pub fn conn_info(&self) -> Option<ConnInfo> {
        self.conn.lock().unwrap().clone()
    }

    /// Ask the supervision loop to shut the daemon down cleanly.
    ///
    /// Does not clear [`Self::child_pid`]: `RunEvent::Exit` may fire
    /// immediately after this and still needs the group leader for
    /// [`best_effort_kill`].
    pub fn request_shutdown(&self) {
        self.shutdown.store(true, Ordering::SeqCst);
        self.shutdown_notify.notify_waiters();
        self.set_status(DaemonStatus::Stopping);
    }

    /// PID of the live daemon child, if known (for the `Exit` backstop).
    fn child_pid(&self) -> Option<u32> {
        let pid = self.child_pid.load(Ordering::Relaxed);
        (pid > 0).then_some(pid as u32)
    }

    fn set_status(&self, s: DaemonStatus) {
        let _ = self.status_tx.send(s);
    }

    fn set_conn(&self, c: Option<ConnInfo>) {
        *self.conn.lock().unwrap() = c;
    }

    pub async fn wait_for_done(&self) {
        self.done.notified().await;
    }
}

/// Spawn the supervision loop for the given app handle and data dir.
pub fn start(app: AppHandle, data_dir: PathBuf) -> DaemonHandle {
    coworker::ensure_coworker_file(&data_dir);
    let (status_tx, status_rx) = watch::channel(DaemonStatus::Starting);
    let handle = DaemonHandle {
        app,
        data_dir,
        status_tx,
        status_rx,
        shutdown: Arc::new(AtomicBool::new(false)),
        shutdown_notify: Arc::new(Notify::new()),
        conn: Arc::new(Mutex::new(None)),
        child_pid: Arc::new(AtomicI32::new(-1)),
        done: Arc::new(Notify::new()),
    };
    let loop_handle = handle.clone();
    tauri::async_runtime::spawn(async move {
        run_supervision(loop_handle).await;
    });
    handle
}

/// Default platform data directory, matching `core/tstd/logging.user_data_dir`.
pub fn data_dir() -> PathBuf {
    #[cfg(target_os = "macos")]
    let base = dirs::data_dir().unwrap_or_else(|| PathBuf::from("."));
    #[cfg(not(target_os = "macos"))]
    let base = dirs::data_dir().unwrap_or_else(|| PathBuf::from("."));
    base.join("com.thatsimpletech.tstdesk")
}

/// Continuous spawn → connect → watch → restart loop, until asked to stop.
async fn run_supervision(h: DaemonHandle) {
    let mut restart: u32 = 0;
    loop {
        // Ask to stop may already be set before this loop body runs.
        if h.shutdown.load(Ordering::SeqCst) {
            break;
        }

        emit(&h, "starting", None, 0);

        let (mut watch, mut ws, port_file, pid) = match acquire_daemon(&h.data_dir).await {
            Ok(acquired) => acquired,
            Err(e) => {
                log::error!("{e}");
                if !bump_restart(&h, &mut restart).await {
                    break;
                }
                continue;
            }
        };
        h.child_pid.store(pid as i32, Ordering::Relaxed);

        // Success: the daemon is up and we are connected.
        restart = 0;
        h.set_conn(Some(ConnInfo {
            port: port_file.port,
            token: port_file.token,
        }));
        h.set_status(DaemonStatus::Connected {
            port: port_file.port,
        });
        emit(&h, "connected", Some(port_file.port), 0);

        // Supervise: the listener exits, or a shutdown request from the host.
        let shutdown_req = h.shutdown_notify.clone();
        let shutdown_wait = async {
            while !h.shutdown.load(Ordering::SeqCst) {
                shutdown_req.notified().await;
            }
        };
        tokio::pin!(shutdown_wait);
        tokio::select! {
            () = watch.wait_exit(pid) => {
                log::info!("daemon exited under supervision (pid {pid})");
                // Bootloader may have exited while the grandchild still
                // listens — reap the group before we spawn another.
                if matches!(watch, ChildWatch::Spawned { .. }) {
                    daemon_pid::kill_spawned_group(pid);
                }
            }
            _ = &mut shutdown_wait => {
                h.set_status(DaemonStatus::Stopping);
                emit(&h, "stopping", None, 0);
                graceful_shutdown(&h, &mut ws, &mut watch, pid).await;
                h.set_conn(None);
                h.child_pid.store(-1, Ordering::Relaxed);
                h.set_status(DaemonStatus::Stopped);
                emit(&h, "stopped", None, 0);
                h.done.notify_waiters();
                return;
            }
        }

        // The child exited. If the host is quitting we are done already;
        // otherwise it crashed and we restart, up to MAX_RESTARTS times.
        if !bump_restart(&h, &mut restart).await {
            break;
        }
    }

    h.set_conn(None);
    h.child_pid.store(-1, Ordering::Relaxed);
    h.set_status(DaemonStatus::Stopped);
    h.done.notify_waiters();
}

/// Run one backoff step after a crash. Returns false when retries are spent.
async fn bump_restart(h: &DaemonHandle, restart: &mut u32) -> bool {
    *restart += 1;
    h.set_conn(None);
    if *restart > MAX_RESTARTS {
        log::error!("tstd kept crashing; giving up after {MAX_RESTARTS} restarts");
        h.set_status(DaemonStatus::Stopped);
        emit(h, "stopped", None, *restart);
        return false;
    }
    h.set_status(DaemonStatus::Crashed { restart: *restart });
    emit(h, "crashed", None, *restart);
    // 500 ms · attempt → 500 ms, 1 s, 1.5 s before the first three restarts.
    tokio::time::sleep(crash_backoff(*restart)).await;
    true
}

fn crash_backoff(restart: u32) -> Duration {
    Duration::from_millis(500 * restart as u64)
}

/// Resolve the `tstd` command. Order: `$TSTD_PATH`, the bundled sidecar
/// (TD-1301), `tstd` on PATH, and, in debug (dev) builds, `uv run
/// --directory <repo>/core tstd` as a fallback.
fn resolve_command() -> Result<Vec<String>, String> {
    let env = |name: &str| std::env::var(name).ok().filter(|v| !v.trim().is_empty());
    let which = |name: &str| which::which(name).ok();
    resolve_with(env, which, bundled_sidecar)
}

/// The PyInstaller-built daemon shipped inside the app bundle (TD-1301).
/// Tauri copies `externalBin` entries next to the app executable under
/// their plain name, so the lookup is an exe-sibling `tstd`.
fn bundled_sidecar() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let candidate = exe
        .parent()?
        .join(format!("tstd{}", std::env::consts::EXE_SUFFIX));
    candidate.is_file().then_some(candidate)
}

fn resolve_with(
    env: impl Fn(&str) -> Option<String>,
    which: impl Fn(&str) -> Option<PathBuf>,
    sidecar: impl Fn() -> Option<PathBuf>,
) -> Result<Vec<String>, String> {
    if let Some(p) = env("TSTD_PATH") {
        return Ok(vec![p]);
    }
    if let Some(p) = sidecar() {
        return Ok(vec![p.display().to_string()]);
    }
    if let Some(p) = which("tstd") {
        return Ok(vec![p.display().to_string()]);
    }
    #[cfg(debug_assertions)]
    {
        // Dev fallback: the core workspace's own venv. Prefer the shebang
        // script over `uv run` so we skip a wrapper, but TD-1304 accepts a
        // descendant either way (the packaged onefile sidecar is one).
        let core = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("core");
        let venv_tstd = core.join(".venv").join("bin").join("tstd");
        if venv_tstd.exists() {
            return Ok(vec![venv_tstd.display().to_string()]);
        }
        Ok(vec![
            "uv".into(),
            "run".into(),
            "-q".into(),
            "--directory".into(),
            core.display().to_string(),
            "tstd".into(),
        ])
    }
    #[cfg(not(debug_assertions))]
    Err("tstd not found; set TSTD_PATH or add it to PATH".into())
}

/// How the host is watching a live `tstd`: a child it spawned, or a
/// leftover listener it attached to (TD-2902).
enum ChildWatch {
    Spawned { child: tokio::process::Child },
    Attached,
}

impl ChildWatch {
    async fn wait_exit(&mut self, pid: u32) {
        match self {
            Self::Spawned { child } => {
                let _ = child.wait().await;
            }
            Self::Attached => wait_pid_gone(pid).await,
        }
    }
}

/// Attach when `port.json` names a live listener; otherwise spawn.
async fn acquire_daemon(
    data_dir: &Path,
) -> Result<(ChildWatch, ClientWs, PortFile, u32), String> {
    if let Some(pf) = live_port_file(data_dir) {
        log::info!(
            "tstd already running at port {} (pid {}); attaching",
            pf.port,
            pf.pid
        );
        let pid = pf.pid;
        let ws = connect_handshake(pf.port, &pf.token).await?;
        return Ok((ChildWatch::Attached, ws, pf, pid));
    }

    let mut child = spawn_daemon(data_dir)?;
    let Some(pid) = child.id() else {
        log::warn!("daemon exited before handing over its pid");
        let _ = child.wait().await;
        return Err("daemon exited before handing over its pid".into());
    };
    let port_file = match wait_for_port_file(data_dir, pid, PORT_FILE_TIMEOUT).await {
        Ok(pf) => pf,
        Err(e) => {
            reap_tree(&mut child, pid).await;
            return Err(e);
        }
    };
    let ws = match connect_handshake(port_file.port, &port_file.token).await {
        Ok(ws) => ws,
        Err(e) => {
            reap_tree(&mut child, pid).await;
            return Err(e);
        }
    };
    Ok((ChildWatch::Spawned { child }, ws, port_file, pid))
}

/// `port.json` when it names a live process. Handshake is the attach
/// proof; this only decides "do not spawn a second tstd".
pub fn live_port_file(dir: &Path) -> Option<PortFile> {
    let raw = std::fs::read_to_string(dir.join("port.json")).ok()?;
    let pf: PortFile = serde_json::from_str(&raw).ok()?;
    if should_spawn_new_daemon(pf.pid, pid_is_alive(pf.pid)) {
        None
    } else {
        Some(pf)
    }
}

async fn wait_pid_gone(pid: u32) {
    loop {
        if !pid_is_alive(pid) {
            return;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

/// Spawn the daemon child. `--parent-pid` is passed only when coworker
/// mode is off (TD-1002 / TD-2905 off path).
pub fn spawn_daemon(data_dir: &Path) -> Result<tokio::process::Child, String> {
    let argv = resolve_command()?;
    let coworker_on = load_coworker(data_dir);
    let mut cmd = tokio::process::Command::new(&argv[0]);
    cmd.args(&argv[1..])
        .arg("--data-dir")
        .arg(data_dir)
        .args(parent_pid_argv(coworker_on, std::process::id()))
        .arg("--log-level")
        .arg("INFO")
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    daemon_pid::apply_process_group(&mut cmd);
    cmd.spawn().map_err(|e| format!("spawn failed: {e}"))
}

/// Wait for a port file whose pid is the spawned child *or a descendant*.
///
/// Keying on the process tree matters after a crash: the previous daemon's
/// file lingers (SIGKILL leaves no clean removal), so mere existence may
/// point at a dead daemon. Exact equality is not enough — the packaged
/// onefile sidecar writes the grandchild's pid (TD-1304).
pub async fn wait_for_port_file(
    dir: &Path,
    pid: u32,
    timeout: Duration,
) -> Result<PortFile, String> {
    let path = dir.join("port.json");
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if let Ok(raw) = tokio::fs::read_to_string(&path).await {
            if let Ok(pf) = serde_json::from_str::<PortFile>(&raw) {
                if port_file_belongs_to_spawn(pid, pf.pid) {
                    return Ok(pf);
                }
            }
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(format!(
                "timed out waiting for a port file belonging to pid {pid} (or a descendant) at {}",
                path.display()
            ));
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

/// Open a WebSocket to the daemon and complete the token handshake.
pub async fn connect_handshake(port: u16, token: &str) -> Result<ClientWs, String> {
    let url = format!("ws://127.0.0.1:{port}");
    let (mut ws, _) = tokio_tungstenite::connect_async(url)
        .await
        .map_err(|e| format!("connect failed: {e}"))?;

    let hello = serde_json::json!({
        "type": "hello",
        "token": token,
        "version": PROTOCOL_VERSION,
    });
    ws.send(Message::Text(hello.to_string()))
        .await
        .map_err(|e| format!("send hello failed: {e}"))?;

    let ack = tokio::time::timeout(HANDSHAKE_TIMEOUT, ws.next())
        .await
        .map_err(|_| "timed out waiting for hello_ack".to_string())?
        .ok_or_else(|| "connection closed during handshake".to_string())?
        .map_err(|e| format!("read hello_ack failed: {e}"))?;

    match ack {
        Message::Text(text) => {
            let v: serde_json::Value = serde_json::from_str(text.as_str())
                .map_err(|e| format!("bad hello_ack json: {e}"))?;
            if v["type"] == "hello_ack" {
                Ok(ws)
            } else {
                Err(format!("expected hello_ack, got {}", v["type"]))
            }
        }
        Message::Close(_) => Err("daemon closed during handshake".into()),
        other => Err(format!("unexpected handshake frame: {other:?}")),
    }
}

/// Send `shutdown` over WS, wait a short grace, then kill the process group.
async fn graceful_shutdown(
    h: &DaemonHandle,
    ws: &mut ClientWs,
    watch: &mut ChildWatch,
    spawned_pid: u32,
) {
    h.set_status(DaemonStatus::Stopping);
    let _ = ws
        .send(Message::Text(r#"{"type":"shutdown"}"#.into()))
        .await;
    // No graceful close handshake: the daemon tears down its own connection
    // DURING shutdown, so waiting for a close ack can race it into a hang.

    match watch {
        ChildWatch::Spawned { child } => {
            match tokio::time::timeout(SHUTDOWN_GRACE, child.wait()).await {
                Ok(Ok(_status)) => log::info!("daemon exited cleanly after shutdown"),
                Ok(Err(e)) => log::warn!("daemon wait errored after shutdown: {e}"),
                Err(_elapsed) => {
                    log::warn!("daemon did not exit within grace; killing the process group");
                }
            }
            reap_tree(child, spawned_pid).await;
        }
        ChildWatch::Attached => {
            match tokio::time::timeout(SHUTDOWN_GRACE, wait_pid_gone(spawned_pid)).await {
                Ok(()) => log::info!("attached daemon exited cleanly after shutdown"),
                Err(_elapsed) => {
                    log::warn!("attached daemon did not exit within grace; killing");
                }
            }
            daemon_pid::kill_spawned_group(spawned_pid);
        }
    }
}

/// SIGKILL the spawned process group, then wait on the leader.
async fn reap_tree(child: &mut tokio::process::Child, spawned_pid: u32) {
    daemon_pid::kill_spawned_group(spawned_pid);
    let _ = child.wait().await;
}

/// Synchronous, best-effort kill of the daemon tree, for `RunEvent::Exit`.
/// The daemon's own parent-pid watchdog is the real backstop; this only
/// closes the small window before the OS reaps us.
pub fn best_effort_kill(handle: &DaemonHandle) {
    if let Some(pid) = handle.child_pid() {
        daemon_pid::kill_spawned_group(pid);
    }
}

fn emit(h: &DaemonHandle, state: &str, port: Option<u16>, restart: u32) {
    let _ = h.app.emit(
        "daemon-status",
        serde_json::json!({
            "state": state,
            "port": port,
            "restart": restart,
            "status": h.status().state_str(),
        }),
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_scales_linearly() {
        assert_eq!(crash_backoff(1), Duration::from_millis(500));
        assert_eq!(crash_backoff(2), Duration::from_millis(1000));
        assert_eq!(crash_backoff(3), Duration::from_millis(1500));
    }

    #[test]
    fn parses_port_file_with_pid() {
        let pf: PortFile =
            serde_json::from_str(r#"{"port": 5111, "token": "deadbeef", "pid": 4242}"#).unwrap();
        assert_eq!(pf.port, 5111);
        assert_eq!(pf.token, "deadbeef");
        assert_eq!(pf.pid, 4242);
    }

    #[test]
    fn port_file_pid_defaults_to_zero_when_absent() {
        let pf: PortFile = serde_json::from_str(r#"{"port": 7, "token": "x"}"#).unwrap();
        assert_eq!(pf.pid, 0);
        assert_ne!(pf.pid, std::process::id()); // never matches a live child
    }

    #[test]
    fn tstd_path_env_wins_resolution() {
        let argv = resolve_with(
            |name| (name == "TSTD_PATH").then(|| "/custom/tstd".into()),
            |_| None,
            || Some(PathBuf::from("/bundle/tstd")),
        )
        .unwrap();
        assert_eq!(argv, vec!["/custom/tstd".to_string()]);
    }

    #[test]
    fn bundled_sidecar_wins_over_path() {
        // The version-locked binary inside the bundle beats whatever a
        // user happens to have installed on PATH (TD-1301).
        let argv = resolve_with(
            |_| None,
            |name| (name == "tstd").then(|| PathBuf::from("/usr/local/bin/tstd")),
            || Some(PathBuf::from("/bundle/tstd")),
        )
        .unwrap();
        assert_eq!(argv, vec!["/bundle/tstd".to_string()]);
    }

    #[test]
    fn which_wins_over_uv_fallback() {
        let argv = resolve_with(
            |_| None,
            |name| (name == "tstd").then(|| PathBuf::from("/usr/local/bin/tstd")),
            || None,
        )
        .unwrap();
        assert_eq!(argv, vec!["/usr/local/bin/tstd".to_string()]);
    }

    #[test]
    fn debug_fallback_resolves_something_when_neither_resolvable() {
        // In debug builds the fallback is the core venv's tstd (preferred)
        // or `uv run ... tstd`. Either way the last token names tstd.
        #[cfg(debug_assertions)]
        {
            let argv = resolve_with(|_| None, |_| None, || None).unwrap();
            assert!(!argv.is_empty());
            assert!(argv.last().unwrap().ends_with("tstd"));
        }
    }

    #[test]
    fn live_port_file_attaches_when_pid_is_this_process() {
        let dir = std::env::temp_dir().join(format!(
            "tstd-live-port-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let pid = std::process::id();
        std::fs::write(
            dir.join("port.json"),
            format!(r#"{{"port": 5111, "token": "deadbeef", "pid": {pid}}}"#),
        )
        .unwrap();
        let pf = live_port_file(&dir).expect("live pid must attach, not spawn");
        assert_eq!(pf.pid, pid);
        assert!(!should_spawn_new_daemon(pf.pid, pid_is_alive(pf.pid)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn live_port_file_ignores_dead_or_zero_pid() {
        let dir = std::env::temp_dir().join(format!(
            "tstd-dead-port-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("port.json"),
            r#"{"port": 7, "token": "x", "pid": 0}"#,
        )
        .unwrap();
        assert!(live_port_file(&dir).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
