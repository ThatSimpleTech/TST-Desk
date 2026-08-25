//! Daemon supervision (TD-1002).
//!
//! The Tauri host spawns the `tstd` Python daemon on launch, discovers it
//! through its port file (`{port, token, pid}`), completes a WebSocket
//! `hello` handshake, watches for crash, and restarts it with a bounded
//! backoff. The matching side of each contract lives in `core/tstd`:
//!
//! - port file: `core/tstd/ws.py` (`create_port_file_path`, `write_port_file`)
//! - handshake + shutdown messages: `core/tstd/protocol.py`
//! - orphan watchdog: `core/tstd/daemon.py` (`--parent-pid`)
//! - coworker flag: `{data_dir}/coworker.yaml` (TD-2902)
//!
//! Quit sends `shutdown` and best-effort-kills from `RunEvent::Exit`.
//! Close hides the window and leaves the host (and `tstd`) running when
//! coworker mode is on. Spawn always passes `--parent-pid`: close does
//! not kill the host, so the watchdog stays quiet; SIGKILL of the host
//! is what reaps an orphan.
//!
//! PyInstaller's `--onefile` sidecar (TD-1301) is a bootloader that spawns
//! the real daemon as a child. Port-file matching and group-kill live in
//! [`daemon_pid`] (TD-1304).

pub mod close_hint;
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

pub use close_hint::{take_first_close_hint, CLOSE_HINT_BODY, CLOSE_HINT_EVENT, CLOSE_HINT_TITLE};
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
///
/// Linux / BSD use the XDG leaf `tst-desk`. macOS and Windows use the
/// Tauri identifier. A host that joined the identifier on Linux split
/// GUI state from `tst run` / `tstd`.
pub fn data_dir() -> PathBuf {
    let base = dirs::data_dir().unwrap_or_else(|| PathBuf::from("."));
    resolve_data_dir(&base)
}

#[cfg(any(
    target_os = "linux",
    target_os = "freebsd",
    target_os = "netbsd",
    target_os = "openbsd"
))]
const DATA_DIR_NAME: &str = "tst-desk";
#[cfg(not(any(
    target_os = "linux",
    target_os = "freebsd",
    target_os = "netbsd",
    target_os = "openbsd"
)))]
const DATA_DIR_NAME: &str = "com.thatsimpletech.tstdesk";

#[cfg(any(
    target_os = "linux",
    target_os = "freebsd",
    target_os = "netbsd",
    target_os = "openbsd"
))]
const LINUX_LEGACY_DATA_DIR_NAME: &str = "com.thatsimpletech.tstdesk";

/// Resolve the product data dir under *base*. Linux renames the reverse-DNS
/// leftover once when `tst-desk` is absent.
pub(crate) fn resolve_data_dir(base: &Path) -> PathBuf {
    let canonical = base.join(DATA_DIR_NAME);
    #[cfg(any(
        target_os = "linux",
        target_os = "freebsd",
        target_os = "netbsd",
        target_os = "openbsd"
    ))]
    {
        migrate_linux_data_dir(base, &canonical)
    }
    #[cfg(not(any(
        target_os = "linux",
        target_os = "freebsd",
        target_os = "netbsd",
        target_os = "openbsd"
    )))]
    {
        canonical
    }
}

#[cfg(any(
    target_os = "linux",
    target_os = "freebsd",
    target_os = "netbsd",
    target_os = "openbsd"
))]
fn migrate_linux_data_dir(base: &Path, canonical: &Path) -> PathBuf {
    let legacy = base.join(LINUX_LEGACY_DATA_DIR_NAME);
    if canonical.exists() || !legacy.exists() {
        return canonical.to_path_buf();
    }
    match std::fs::rename(&legacy, canonical) {
        Ok(()) => canonical.to_path_buf(),
        Err(_) => legacy,
    }
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

/// Resolve the `tstd` command.
///
/// Dev (`dev_overrides`): `$TSTD_PATH`, then the core venv / `uv run`.
/// Release: the bundled sidecar (TD-1301), then `tstd` on PATH. `$TSTD_PATH`
/// is ignored — a release binary must not run whatever the environment
/// names (TD-4809). Dev never falls through to the sidecar so `tauri
/// dev` keeps tracking the checkout rather than latching onto a stale
/// packaged binary once externalBin ships one next to the dev executable.
fn resolve_command() -> Result<Vec<String>, String> {
    let env = |name: &str| std::env::var(name).ok().filter(|v| !v.trim().is_empty());
    let which = |name: &str| which::which(name).ok();
    resolve_with(env, which, bundled_sidecar, cfg!(debug_assertions))
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

/// `dev_overrides` mirrors `cfg!(debug_assertions)` at the real call site
/// and is a plain argument so tests can exercise release semantics from
/// the debug toolchain. Dev-only resolution (`$TSTD_PATH`, the core venv,
/// `uv run`) is additionally compiled out of release builds.
fn resolve_with(
    env: impl Fn(&str) -> Option<String>,
    which: impl Fn(&str) -> Option<PathBuf>,
    sidecar: impl Fn() -> Option<PathBuf>,
    dev_overrides: bool,
) -> Result<Vec<String>, String> {
    if dev_overrides {
        if let Some(p) = env("TSTD_PATH") {
            return Ok(vec![p]);
        }
        #[cfg(debug_assertions)]
        {
            // Dev fallback: the core workspace's own venv. Prefer the
            // shebang script over `uv run` so we skip a wrapper, but
            // TD-1304 accepts a descendant either way (the packaged
            // onefile sidecar is one). This arm always resolves, so dev
            // never falls through to the sidecar or PATH.
            let core = Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .join("core");
            let venv_tstd = core.join(".venv").join("bin").join("tstd");
            if venv_tstd.exists() {
                return Ok(vec![venv_tstd.display().to_string()]);
            }
            return Ok(vec![
                "uv".into(),
                "run".into(),
                "-q".into(),
                "--directory".into(),
                core.display().to_string(),
                "tstd".into(),
            ]);
        }
    }
    if let Some(p) = sidecar() {
        return Ok(vec![p.display().to_string()]);
    }
    if let Some(p) = which("tstd") {
        return Ok(vec![p.display().to_string()]);
    }
    Err("tstd not found; the bundled daemon is missing and nothing named tstd is on PATH".into())
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
async fn acquire_daemon(data_dir: &Path) -> Result<(ChildWatch, ClientWs, PortFile, u32), String> {
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

/// Spawn the daemon child. `--parent-pid` is always the host pid
/// (TD-2903): close keeps this process alive; force-quit is the orphan
/// backstop.
pub fn spawn_daemon(data_dir: &Path) -> Result<tokio::process::Child, String> {
    spawn_daemon_with(data_dir, &resolve_command()?)
}

/// Same spawn with the command chosen by the caller instead of resolved
/// from the environment. Test seam (TD-4810): the onefile-shape test used
/// to point process-wide `TSTD_PATH` at its wrapper around the spawn,
/// which raced sibling tests' `spawn_daemon` under parallel threads.
pub fn spawn_daemon_with(
    data_dir: &Path,
    argv: &[String],
) -> Result<tokio::process::Child, String> {
    let mut cmd = tokio::process::Command::new(&argv[0]);
    cmd.args(&argv[1..])
        .arg("--data-dir")
        .arg(data_dir)
        .args(parent_pid_argv(std::process::id()))
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
    ws.send(Message::Text(hello.to_string().into()))
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
    fn tstd_path_env_wins_dev_resolution() {
        let argv = resolve_with(
            |name| (name == "TSTD_PATH").then(|| "/custom/tstd".into()),
            |_| None,
            || Some(PathBuf::from("/bundle/tstd")),
            true,
        )
        .unwrap();
        assert_eq!(argv, vec!["/custom/tstd".to_string()]);
    }

    #[test]
    fn dev_prefers_source_environment_over_sidecar() {
        // Once externalBin ships, `tauri dev` finds a packaged binary next
        // to the dev executable. Dev must keep tracking the checkout, not
        // latch onto it (TD-4809).
        let argv = resolve_with(
            |_| None,
            |_| None,
            || Some(PathBuf::from("/bundle/tstd")),
            true,
        )
        .unwrap();
        assert_ne!(argv, vec!["/bundle/tstd".to_string()]);
    }

    #[test]
    fn bundled_sidecar_wins_over_path_in_release() {
        // The version-locked binary inside the bundle beats whatever a
        // user happens to have installed on PATH (TD-1301) — and beats a
        // TSTD_PATH pointing elsewhere (TD-4809).
        let argv = resolve_with(
            |name| (name == "TSTD_PATH").then(|| "/custom/tstd".into()),
            |name| (name == "tstd").then(|| PathBuf::from("/usr/local/bin/tstd")),
            || Some(PathBuf::from("/bundle/tstd")),
            false,
        )
        .unwrap();
        assert_eq!(argv, vec!["/bundle/tstd".to_string()]);
    }

    #[test]
    fn release_build_refuses_tstd_path() {
        // A release binary runs its bundled daemon or PATH, never whatever
        // the environment names (TD-4809). Nothing resolvable but
        // TSTD_PATH → refuse rather than honor it.
        let result = resolve_with(
            |name| (name == "TSTD_PATH").then(|| "/custom/tstd".into()),
            |_| None,
            || None,
            false,
        );
        assert!(result.is_err());
    }

    #[test]
    fn release_build_falls_back_to_path_without_sidecar() {
        let argv = resolve_with(
            |_| None,
            |name| (name == "tstd").then(|| PathBuf::from("/usr/local/bin/tstd")),
            || None,
            false,
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
            let argv = resolve_with(|_| None, |_| None, || None, true).unwrap();
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

    #[test]
    fn data_dir_leaf_matches_python() {
        let base = std::env::temp_dir().join(format!(
            "tstd-data-leaf-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&base).unwrap();
        let resolved = resolve_data_dir(&base);
        assert_eq!(resolved, base.join(DATA_DIR_NAME));
        assert!(!resolved.exists());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[cfg(any(
        target_os = "linux",
        target_os = "freebsd",
        target_os = "netbsd",
        target_os = "openbsd"
    ))]
    #[test]
    fn linux_data_dir_renames_reverse_dns_leftover() {
        let base = std::env::temp_dir().join(format!(
            "tstd-data-mig-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let legacy = base.join("com.thatsimpletech.tstdesk");
        std::fs::create_dir_all(&legacy).unwrap();
        std::fs::write(legacy.join("port.json"), "{}").unwrap();
        let resolved = resolve_data_dir(&base);
        assert_eq!(resolved, base.join("tst-desk"));
        assert!(resolved.join("port.json").is_file());
        assert!(!legacy.exists());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[cfg(any(
        target_os = "linux",
        target_os = "freebsd",
        target_os = "netbsd",
        target_os = "openbsd"
    ))]
    #[test]
    fn linux_data_dir_does_not_merge_two_live_trees() {
        let base = std::env::temp_dir().join(format!(
            "tstd-data-both-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let canonical = base.join("tst-desk");
        let legacy = base.join("com.thatsimpletech.tstdesk");
        std::fs::create_dir_all(&canonical).unwrap();
        std::fs::create_dir_all(&legacy).unwrap();
        std::fs::write(canonical.join("config.yaml"), "canonical: true\n").unwrap();
        std::fs::write(legacy.join("port.json"), "{}").unwrap();
        let resolved = resolve_data_dir(&base);
        assert_eq!(resolved, canonical);
        assert!(canonical.join("config.yaml").is_file());
        assert!(legacy.join("port.json").is_file());
        let _ = std::fs::remove_dir_all(&base);
    }
}
