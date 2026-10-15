//! Claude-style updater (TD-4837).
//!
//! A packaged release build checks GitHub on launch and every four hours.
//! The download is the platform asset from a `tstdesk-v*` release, and it
//! is installed only when `SHA256SUMS.txt` matches. Debug builds do not
//! poll: `tauri dev` would otherwise hit GitHub on every reload, and a
//! `target/` binary must not replace itself.
//!
//! The request carries no install id. Apple signing is still deferred;
//! the checksum is the check the release notes already ask for.

mod apply;
mod drive;
mod fetch;
mod plan;

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

use apply::{file_sha256, spawn_helper};
use plan::{load_automatic, store_automatic, ChosenAsset, PackageKind};

pub struct UpdateMenu(pub tauri::menu::MenuItem<tauri::Wry>);

#[derive(Clone, Serialize)]
pub struct UpdateStatus {
    pub phase: String,
    pub current: String,
    pub latest: Option<String>,
    pub release_url: Option<String>,
    pub detail: String,
    pub automatic: bool,
    pub can_apply: bool,
    pub can_open_installer: bool,
    pub announce: bool,
}

#[derive(Clone)]
struct Pending {
    version: String,
    release_url: String,
    asset: ChosenAsset,
}

#[derive(Clone)]
struct Staged {
    version: String,
    release_url: String,
    kind: PackageKind,
    payload: PathBuf,
    sha256: String,
    prepared: PathBuf,
    dest: Option<PathBuf>,
    can_apply: bool,
    can_open_installer: bool,
}

struct Inner {
    busy: bool,
    status: UpdateStatus,
    pending: Option<Pending>,
    staged: Option<Staged>,
}

#[derive(Clone)]
pub struct Updater {
    inner: Arc<Mutex<Inner>>,
}

impl Updater {
    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn snapshot(&self) -> UpdateStatus {
        self.lock().status.clone()
    }

    fn from_app(app: &AppHandle) -> Self {
        app.state::<Updater>().inner.clone().into()
    }

    fn emit(&self, app: &AppHandle, status: UpdateStatus) {
        let prev = {
            let mut guard = self.lock();
            let prev = guard.status.phase.clone();
            guard.busy = status.phase == "checking" || status.phase == "downloading";
            guard.status = status.clone();
            prev
        };
        if status.phase == "ready" && prev != "ready" {
            let _ = app
                .notification()
                .builder()
                .title("Update ready")
                .body(&status.detail)
                .show();
        }
        if let Some(item) = app.try_state::<UpdateMenu>() {
            let label = if status.phase == "ready" && status.can_apply {
                "Restart to Update…"
            } else {
                "Check for Updates…"
            };
            let _ = item.0.set_text(label);
        }
        let _ = app.emit("update-status", &status);
    }

    fn base(&self) -> UpdateStatus {
        let cur = self.snapshot();
        UpdateStatus {
            phase: cur.phase,
            current: cur.current,
            latest: None,
            release_url: None,
            detail: String::new(),
            automatic: cur.automatic,
            can_apply: false,
            can_open_installer: false,
            announce: false,
        }
    }

    /// False when a check or download is already running.
    fn begin(&self, app: &AppHandle, interactive: bool) -> bool {
        let status = {
            let mut guard = self.lock();
            if guard.busy {
                return false;
            }
            guard.busy = true;
            guard.status.phase = "checking".into();
            guard.status.detail = "Checking GitHub releases…".into();
            guard.status.announce = interactive;
            guard.status.can_apply = false;
            guard.status.can_open_installer = false;
            guard.status.clone()
        };
        let _ = app.emit("update-status", &status);
        true
    }

    fn fail(&self, app: &AppHandle, interactive: bool, err: &str) {
        let mut status = self.base();
        status.phase = if interactive { "error" } else { "idle" }.into();
        status.detail = err.to_string();
        status.announce = interactive;
        self.emit(app, status);
    }

    pub async fn restart(&self, app: &AppHandle) -> Result<UpdateStatus, String> {
        let staged = self
            .lock()
            .staged
            .clone()
            .ok_or_else(|| "there is no update ready to install".to_string())?;
        if !staged.can_apply {
            return Err("this update opens in the system installer".into());
        }
        let payload = staged.payload.clone();
        let expected = staged.sha256.clone();
        let actual = tokio::task::spawn_blocking(move || file_sha256(&payload))
            .await
            .map_err(|e| format!("update task stopped: {e}"))??;
        if actual != expected {
            self.fail(app, true, plan::checksum_failed_detail());
            return Err(plan::checksum_failed_detail().into());
        }
        let updates = crate::daemon::data_dir().join("updates");
        spawn_helper(
            staged.kind,
            &updates,
            &staged.prepared,
            staged.dest.as_deref(),
        )?;
        crate::request_quit(app);
        Ok(self.snapshot())
    }

    pub fn open_installer(&self, app: &AppHandle) -> Result<(), String> {
        let prepared = self
            .lock()
            .staged
            .as_ref()
            .filter(|s| s.can_open_installer || s.can_apply)
            .map(|s| s.prepared.clone())
            .ok_or_else(|| "there is no installer to open".to_string())?;
        app.opener()
            .open_path(prepared.to_string_lossy().to_string(), None::<&str>)
            .map_err(|e| format!("couldn't open the installer: {e}"))
    }

    pub async fn set_automatic(&self, app: &AppHandle, on: bool) -> Result<UpdateStatus, String> {
        store_automatic(&crate::daemon::data_dir(), on)?;
        {
            let mut guard = self.lock();
            guard.status.automatic = on;
        }
        let status = self.snapshot();
        let _ = app.emit("update-status", &status);
        if on {
            let _ = self.check(app, false).await;
        }
        Ok(self.snapshot())
    }
}

impl From<Arc<Mutex<Inner>>> for Updater {
    fn from(inner: Arc<Mutex<Inner>>) -> Self {
        Self { inner }
    }
}

fn reset_stage(data_dir: &Path) -> PathBuf {
    let stage = data_dir.join("updates").join("stage");
    let _ = std::fs::remove_dir_all(&stage);
    let _ = std::fs::create_dir_all(&stage);
    stage
}

pub fn install(app: &AppHandle) {
    let current = app
        .config()
        .version
        .clone()
        .unwrap_or_else(|| env!("CARGO_PKG_VERSION").into());
    let status = UpdateStatus {
        phase: "idle".into(),
        current,
        latest: None,
        release_url: None,
        detail: String::new(),
        automatic: load_automatic(&crate::daemon::data_dir()),
        can_apply: false,
        can_open_installer: false,
        announce: false,
    };
    app.manage(Updater {
        inner: Arc::new(Mutex::new(Inner {
            busy: false,
            status,
            pending: None,
            staged: None,
        })),
    });
}

pub fn start(app: &AppHandle) {
    let _ = std::fs::remove_dir_all(crate::daemon::data_dir().join("updates").join("stage"));
    if cfg!(debug_assertions) {
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(plan::STARTUP_DELAY).await;
        poll(&app).await;
        loop {
            tokio::time::sleep(plan::CHECK_INTERVAL).await;
            poll(&app).await;
        }
    });
}

async fn poll(app: &AppHandle) {
    if !load_automatic(&crate::daemon::data_dir()) {
        return;
    }
    let updater = Updater::from_app(app);
    let _ = updater.check(app, false).await;
}

pub fn on_menu(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let updater = Updater::from_app(&app);
        let snap = updater.snapshot();
        if snap.phase == "ready" && snap.can_apply {
            let _ = updater.restart(&app).await;
        } else {
            let _ = updater.check(&app, true).await;
        }
    });
}

#[tauri::command]
pub async fn update_status(app: AppHandle) -> UpdateStatus {
    Updater::from_app(&app).snapshot()
}

#[tauri::command]
pub async fn check_for_updates(app: AppHandle) -> Result<UpdateStatus, String> {
    Updater::from_app(&app).check(&app, true).await
}

#[tauri::command]
pub async fn download_update(app: AppHandle) -> Result<UpdateStatus, String> {
    Updater::from_app(&app).download(&app).await
}

#[tauri::command]
pub async fn restart_to_update(app: AppHandle) -> Result<UpdateStatus, String> {
    Updater::from_app(&app).restart(&app).await
}

#[tauri::command]
pub async fn open_update_installer(app: AppHandle) -> Result<(), String> {
    Updater::from_app(&app).open_installer(&app)
}

#[tauri::command]
pub async fn set_automatic_updates(app: AppHandle, enabled: bool) -> Result<UpdateStatus, String> {
    Updater::from_app(&app).set_automatic(&app, enabled).await
}
