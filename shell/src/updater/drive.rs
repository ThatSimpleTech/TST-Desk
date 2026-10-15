//! Check and download flow for the updater.

use tauri::AppHandle;

use super::apply::{assess, current_exe, extract_macos_app};
use super::fetch;
use super::plan::{
    self, choose_asset, compare_versions, downloading_detail, manual_detail, newest,
    no_release_detail, parse_sha256sums, ready_detail, unpackaged_detail, up_to_date_detail,
    PackageKind,
};
use super::{reset_stage, Pending, Staged, UpdateStatus, Updater};

impl Updater {
    pub async fn check(&self, app: &AppHandle, interactive: bool) -> Result<UpdateStatus, String> {
        if !self.begin(app, interactive) {
            return Ok(self.snapshot());
        }
        match self.run_check(app, interactive).await {
            Ok(()) => Ok(self.snapshot()),
            Err(err) => {
                // A background check that dies before anything is shown stays
                // quiet. Once the phase is downloading, the banner is already
                // up, so the failure has to replace it.
                let loud = interactive || self.snapshot().phase == "downloading";
                self.fail(app, loud, &err);
                Err(err)
            }
        }
    }

    async fn run_check(&self, app: &AppHandle, interactive: bool) -> Result<(), String> {
        let http = fetch::client()?;
        let releases = fetch::fetch_releases(&http).await?;
        let current = self.snapshot().current;
        let Some(release) = newest(&releases) else {
            self.publish_settled(
                app,
                interactive,
                "up_to_date",
                None,
                None,
                no_release_detail(),
            );
            return Ok(());
        };
        if compare_versions(&current, &release.version) != std::cmp::Ordering::Less {
            self.publish_settled(
                app,
                interactive,
                "up_to_date",
                Some(release.version.clone()),
                Some(release.html_url.clone()),
                up_to_date_detail(),
            );
            return Ok(());
        }
        if self.reuse_staged(app, &release.version) {
            return Ok(());
        }
        let Some(asset) = choose_asset(release, &plan::host_platform()) else {
            self.publish_settled(
                app,
                true,
                "available",
                Some(release.version.clone()),
                Some(release.html_url.clone()),
                &unpackaged_detail(&release.version),
            );
            return Ok(());
        };
        let exe = current_exe()?;
        let target = assess(asset.kind, &exe);
        if !target.packaged || (!target.can_apply && !target.can_open_installer) {
            self.publish_settled(
                app,
                true,
                "available",
                Some(release.version.clone()),
                Some(release.html_url.clone()),
                &unpackaged_detail(&release.version),
            );
            return Ok(());
        }
        {
            let mut guard = self.lock();
            guard.pending = Some(Pending {
                version: release.version.clone(),
                release_url: release.html_url.clone(),
                asset: asset.clone(),
            });
        }
        if !self.snapshot().automatic {
            let mut status = self.base();
            status.phase = "available".into();
            status.latest = Some(release.version.clone());
            status.release_url = Some(release.html_url.clone());
            status.detail = manual_detail(&release.version);
            status.announce = true;
            status.can_apply = target.can_apply;
            status.can_open_installer = target.can_open_installer;
            self.emit(app, status);
            return Ok(());
        }
        self.download_pending(app, &http).await
    }

    fn reuse_staged(&self, app: &AppHandle, version: &str) -> bool {
        let staged = {
            let guard = self.lock();
            guard.staged.clone()
        };
        let Some(staged) = staged else {
            return false;
        };
        if staged.version != version || !staged.payload.is_file() {
            return false;
        }
        self.publish_ready(app, &staged);
        true
    }

    fn publish_settled(
        &self,
        app: &AppHandle,
        announce: bool,
        phase: &str,
        latest: Option<String>,
        release_url: Option<String>,
        detail: &str,
    ) {
        {
            let mut guard = self.lock();
            guard.pending = None;
            if phase == "up_to_date" {
                guard.staged = None;
            }
        }
        let mut status = self.base();
        status.phase = phase.into();
        status.latest = latest;
        status.release_url = release_url;
        status.detail = detail.into();
        status.announce = announce;
        self.emit(app, status);
    }

    pub async fn download(&self, app: &AppHandle) -> Result<UpdateStatus, String> {
        if !self.begin(app, true) {
            return Ok(self.snapshot());
        }
        // begin() flipped the phase to checking. A manual download already
        // knows the release; put the phase back so a failure still reads
        // as a download. The pending asset is what matters.
        let http = match fetch::client() {
            Ok(http) => http,
            Err(err) => {
                self.fail(app, true, &err);
                return Err(err);
            }
        };
        match self.download_pending(app, &http).await {
            Ok(()) => Ok(self.snapshot()),
            Err(err) => {
                self.fail(app, true, &err);
                Err(err)
            }
        }
    }

    async fn download_pending(
        &self,
        app: &AppHandle,
        http: &reqwest::Client,
    ) -> Result<(), String> {
        let pending = self
            .lock()
            .pending
            .clone()
            .ok_or_else(|| "there is no update to download".to_string())?;
        let version = pending.version.clone();
        let stage = reset_stage(&crate::daemon::data_dir());
        let payload = stage.join(&pending.asset.name);
        let sums_body = fetch::download_text(http, &pending.asset.sums_url).await?;
        let sums = parse_sha256sums(&sums_body);
        let expected = sums.get(&pending.asset.name).cloned().ok_or_else(|| {
            format!(
                "SHA256SUMS.txt has no entry for {}. Nothing was installed.",
                pending.asset.name
            )
        })?;
        {
            let mut status = self.base();
            status.phase = "downloading".into();
            status.latest = Some(version.clone());
            status.release_url = Some(pending.release_url.clone());
            status.detail = downloading_detail(&pending.asset.name, None);
            status.announce = true;
            self.emit(app, status);
        }
        let mut last_pct: Option<u8> = None;
        let name = pending.asset.name.clone();
        let version_for_progress = version.clone();
        let release_url = pending.release_url.clone();
        fetch::download_verified(
            http,
            &pending.asset.url,
            &payload,
            &expected,
            |got, total| {
                let pct = total.and_then(|t| (t > 0).then_some(((got * 100) / t) as u8));
                if pct == last_pct {
                    return;
                }
                last_pct = pct;
                let mut status = self.base();
                status.phase = "downloading".into();
                status.latest = Some(version_for_progress.clone());
                status.release_url = Some(release_url.clone());
                status.detail = downloading_detail(&name, pct);
                status.announce = true;
                self.emit(app, status);
            },
        )
        .await?;
        let prepared = match pending.asset.kind {
            PackageKind::MacosDmg => {
                let dmg = payload.clone();
                let dir = stage.clone();
                tokio::task::spawn_blocking(move || extract_macos_app(&dmg, &dir))
                    .await
                    .map_err(|e| format!("update task stopped: {e}"))??
            }
            _ => payload.clone(),
        };
        #[cfg(unix)]
        if pending.asset.kind == PackageKind::AppImage {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&prepared, std::fs::Permissions::from_mode(0o755));
        }
        let exe = current_exe()?;
        let target = assess(pending.asset.kind, &exe);
        let staged = Staged {
            version: version.clone(),
            release_url: pending.release_url.clone(),
            kind: pending.asset.kind,
            payload,
            sha256: expected,
            prepared,
            dest: target.dest.clone(),
            can_apply: target.can_apply,
            can_open_installer: target.can_open_installer,
        };
        {
            let mut guard = self.lock();
            guard.pending = None;
            guard.staged = Some(staged.clone());
        }
        self.publish_ready(app, &staged);
        Ok(())
    }

    fn publish_ready(&self, app: &AppHandle, staged: &Staged) {
        let mut status = self.base();
        status.phase = "ready".into();
        status.latest = Some(staged.version.clone());
        status.release_url = Some(staged.release_url.clone());
        status.detail = ready_detail(&staged.version, staged.kind, staged.can_apply);
        status.can_apply = staged.can_apply;
        status.can_open_installer = staged.can_open_installer;
        status.announce = true;
        self.emit(app, status);
    }
}
