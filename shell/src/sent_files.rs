//! A file the agent sent, as this computer sees it (TD-5144, design §7.1).
//!
//! For a session on this computer the daemon's own copy sits at
//! `<data_dir>/sessions/<sid>/artifacts/<aid>/<name>`, alone in its folder;
//! the card opens it there, shows it in its folder or saves a copy. For a
//! session on another computer the file is downloaded first
//! (`received_files.rs`) and these commands act on the copy in Downloads.
//! Every command takes ids, never a path, so the webview cannot point the
//! opener at anything else, and Open refuses (`not_openable`) any kind
//! outside the allowlist, so a UI slip cannot run a program either.

use std::fs;
use std::path::{Path, PathBuf};

use serde::Serialize;
use tauri::AppHandle;
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

use crate::daemon;
use crate::read_text::{is_within, safe_session_id};
use crate::received_files::{self, io_err, safe_name};

/// What this computer holds of a sent file (§1.6).
#[derive(Serialize, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct FileStatus {
    /// "local", "received", "partial" or "none".
    pub state: &'static str,
    pub have: u64,
}

impl FileStatus {
    pub(crate) fn new(state: &'static str, have: u64) -> Self {
        Self { state, have }
    }
}

/// The only kinds Open hands to the OS on the desktop. Anything else, an
/// agent-made script or program above all, gets Show in folder and Save a
/// copy; an allowlist, because a list of program types is never complete
/// (.js, .vbs, .hta, .lnk, .url, .py ... all run on a double-click).
const OPENABLE_EXTS: [&str; 27] = [
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".mp4", ".webm", ".mov", ".mkv",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".txt", ".md", ".csv", ".log", ".json",
    ".zip", ".tar", ".gz", ".tgz", ".7z",
];

/// The extension the OS acts on is taken after trailing spaces and dots
/// are gone, because Windows drops them when it opens the file.
pub(crate) fn is_openable(name: &str) -> bool {
    let name = name.trim_end_matches([' ', '.']);
    name.rfind('.')
        .is_some_and(|at| OPENABLE_EXTS.contains(&name[at..].to_ascii_lowercase().as_str()))
}

/// `uuid4().hex`: 32 lowercase hex characters (§1.3).
fn is_artifact_id(id: &str) -> bool {
    id.len() == 32
        && id
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Checked before anything else touches the disk.
pub(crate) fn check_id(id: &str) -> Result<(), String> {
    if is_artifact_id(id) {
        Ok(())
    } else {
        Err("bad_id".into())
    }
}

/// The daemon's copy: the one regular file in the artifact's folder, still
/// inside that folder once every link is resolved. A bridged session id
/// (with ':') never has a folder here, so it is never looked up.
pub(crate) fn locate_local_in(
    data_dir: &Path,
    session_id: &str,
    artifact_id: &str,
) -> Option<PathBuf> {
    if !is_artifact_id(artifact_id) {
        return None;
    }
    let sid = safe_session_id(session_id)?;
    let dir = data_dir
        .join("sessions")
        .join(sid)
        .join("artifacts")
        .join(artifact_id);
    if !fs::symlink_metadata(&dir).ok()?.file_type().is_dir() {
        return None;
    }
    let mut entries = fs::read_dir(&dir).ok()?;
    let only = entries.next()?.ok()?.path();
    if entries.next().is_some() {
        return None;
    }
    if !fs::symlink_metadata(&only).ok()?.file_type().is_file() {
        return None;
    }
    let root = fs::canonicalize(&dir).ok()?;
    let file = fs::canonicalize(&only).ok()?;
    if file == root || !is_within(&root, &file) {
        return None;
    }
    Some(file)
}

/// `local` if the daemon's copy is here, else what was received.
pub(crate) fn status_with(
    data_dir: &Path,
    received: Option<&Path>,
    session_id: &str,
    artifact_id: &str,
) -> Result<FileStatus, String> {
    check_id(artifact_id)?;
    if let Some(file) = locate_local_in(data_dir, session_id, artifact_id) {
        let size = fs::metadata(&file).map(|m| m.len()).unwrap_or(0);
        return Ok(FileStatus::new("local", size));
    }
    Ok(received.map_or(FileStatus::new("none", 0), |root| {
        received_files::status_in(root, artifact_id)
    }))
}

/// The file the card's Open, Show in folder and Save a copy act on.
pub(crate) fn target_with(
    data_dir: &Path,
    received: Option<&Path>,
    session_id: &str,
    artifact_id: &str,
) -> Result<PathBuf, String> {
    check_id(artifact_id)?;
    if let Some(file) = locate_local_in(data_dir, session_id, artifact_id) {
        return Ok(file);
    }
    received
        .and_then(|root| received_files::done_path_in(root, artifact_id))
        .ok_or_else(|| "not_found".to_string())
}

/// What Open may hand the OS opener: the card's file, when it is a kind
/// the desktop opens.
pub(crate) fn open_target_with(
    data_dir: &Path,
    received: Option<&Path>,
    session_id: &str,
    artifact_id: &str,
) -> Result<PathBuf, String> {
    let path = target_with(data_dir, received, session_id, artifact_id)?;
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
    if !is_openable(name) {
        return Err("not_openable".into());
    }
    Ok(path)
}

fn target(app: &AppHandle, session_id: &str, artifact_id: &str) -> Result<PathBuf, String> {
    let received = received_files::received_root(app).ok();
    target_with(
        &daemon::data_dir(),
        received.as_deref(),
        session_id,
        artifact_id,
    )
}

/// Copy `source` to `dest`, unless they are the same file: a copy onto
/// itself would truncate it.
fn copy_to(source: &Path, dest: &Path) -> Result<(), String> {
    if let (Ok(a), Ok(b)) = (fs::canonicalize(source), fs::canonicalize(dest)) {
        if a == b {
            return Ok(());
        }
    }
    fs::copy(source, dest).map(|_| ()).map_err(io_err)
}

// ── Commands (§1.6) ────────────────────────────────────────────────────

#[tauri::command(async)]
pub fn sent_file_status(
    app: AppHandle,
    session_id: String,
    artifact_id: String,
) -> Result<FileStatus, String> {
    let received = received_files::received_root(&app).ok();
    status_with(
        &daemon::data_dir(),
        received.as_deref(),
        &session_id,
        &artifact_id,
    )
}

#[tauri::command]
pub fn sent_file_open(
    app: AppHandle,
    session_id: String,
    artifact_id: String,
) -> Result<(), String> {
    let received = received_files::received_root(&app).ok();
    let path = open_target_with(
        &daemon::data_dir(),
        received.as_deref(),
        &session_id,
        &artifact_id,
    )?;
    app.opener()
        .open_path(path.to_string_lossy(), None::<&str>)
        .map_err(|e| format!("no_app: {e}"))
}

#[tauri::command]
pub fn sent_file_reveal(
    app: AppHandle,
    session_id: String,
    artifact_id: String,
) -> Result<(), String> {
    let path = target(&app, &session_id, &artifact_id)?;
    app.opener()
        .reveal_item_in_dir(&path)
        .map_err(|e| format!("no_app: {e}"))
}

/// Ask where to save a copy, starting in Downloads. `None` if cancelled.
#[tauri::command]
pub async fn sent_file_save_as(
    app: AppHandle,
    session_id: String,
    artifact_id: String,
    name: String,
) -> Result<Option<String>, String> {
    let source = target(&app, &session_id, &artifact_id)?;
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = app.dialog().file().set_file_name(safe_name(&name));
        if let Some(dir) = dirs::download_dir() {
            dialog = dialog.set_directory(dir);
        }
        let Some(chosen) = dialog.blocking_save_file() else {
            return Ok(None);
        };
        let dest = chosen.into_path().map_err(|e| format!("io: {e}"))?;
        copy_to(&source, &dest)?;
        Ok(Some(dest.to_string_lossy().into_owned()))
    })
    .await
    .map_err(|e| format!("io: {e}"))?
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::SystemTime;

    const AID: &str = "0123456789abcdef0123456789abcdef";
    const HI_SHA: &str = "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4";

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("tst-sent-{}-{tag}", std::process::id()));
            let _ = fs::remove_dir_all(&dir);
            fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn artifact_dir(data: &Path, sid: &str, aid: &str) -> PathBuf {
        let dir = data.join("sessions").join(sid).join("artifacts").join(aid);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn locate_finds_single_regular_file() {
        let tmp = TempDir::new("locate");
        let dir = artifact_dir(&tmp.0, "sess-1", AID);
        fs::write(dir.join("app-release.apk"), b"hi").unwrap();
        let found = locate_local_in(&tmp.0, "sess-1", AID).unwrap();
        assert_eq!(
            found,
            fs::canonicalize(dir.join("app-release.apk")).unwrap()
        );
        assert_eq!(target_with(&tmp.0, None, "sess-1", AID), Ok(found));
    }

    #[test]
    fn locate_refuses_symlink_two_entries_bad_id_bridged_session() {
        let tmp = TempDir::new("refuse");
        let outside = tmp.0.join("secret.txt");
        fs::write(&outside, b"not sent").unwrap();

        // Two entries: not the daemon's layout.
        let two = artifact_dir(&tmp.0, "sess-1", AID);
        fs::write(two.join("a.txt"), b"a").unwrap();
        fs::write(two.join("b.txt"), b"b").unwrap();
        assert_eq!(locate_local_in(&tmp.0, "sess-1", AID), None);

        // A folder in place of the file.
        let other = "fedcba9876543210fedcba9876543210";
        fs::create_dir_all(artifact_dir(&tmp.0, "sess-1", other).join("nested")).unwrap();
        assert_eq!(locate_local_in(&tmp.0, "sess-1", other), None);

        #[cfg(unix)]
        {
            let link = "11111111111111111111111111111111";
            let dir = artifact_dir(&tmp.0, "sess-1", link);
            std::os::unix::fs::symlink(&outside, dir.join("secret.txt")).unwrap();
            assert_eq!(locate_local_in(&tmp.0, "sess-1", link), None);

            // The artifact folder itself a link out.
            let base = tmp.0.join("sessions/sess-1/artifacts");
            let linked = "22222222222222222222222222222222";
            let elsewhere = tmp.0.join("elsewhere");
            fs::create_dir_all(&elsewhere).unwrap();
            fs::write(elsewhere.join("x.txt"), b"x").unwrap();
            std::os::unix::fs::symlink(&elsewhere, base.join(linked)).unwrap();
            assert_eq!(locate_local_in(&tmp.0, "sess-1", linked), None);
        }

        let good = artifact_dir(&tmp.0, "sess-2", AID);
        fs::write(good.join("hi.txt"), b"hi").unwrap();
        assert!(locate_local_in(&tmp.0, "sess-2", AID).is_some());
        for bad in ["", "../x", "0123456789ABCDEF0123456789ABCDEF", "0123"] {
            assert_eq!(locate_local_in(&tmp.0, "sess-2", bad), None, "{bad:?}");
            assert_eq!(
                status_with(&tmp.0, None, "sess-2", bad),
                Err("bad_id".into())
            );
        }
        for sid in ["sess-2:mch_1a2b3c4d5e6f", "..", "a/b", ""] {
            assert_eq!(locate_local_in(&tmp.0, sid, AID), None, "{sid:?}");
        }
        assert_eq!(
            target_with(&tmp.0, None, "sess-2:mch_1a2b3c4d5e6f", AID),
            Err("not_found".into())
        );
    }

    #[test]
    fn status_reports_local_received_partial_none() {
        let tmp = TempDir::new("status");
        let data = tmp.0.join("data");
        let received = tmp.0.join("received");
        let downloads = tmp.0.join("Downloads");

        let local = artifact_dir(&data, "sess-1", AID);
        fs::write(local.join("hi.txt"), b"hi").unwrap();
        assert_eq!(
            status_with(&data, Some(&received), "sess-1", AID),
            Ok(FileStatus::new("local", 2))
        );

        let bridged = "sess-9:mch_1a2b3c4d5e6f";
        assert_eq!(
            status_with(&data, Some(&received), bridged, AID),
            Ok(FileStatus::new("none", 0))
        );
        received_files::begin_in(
            &received,
            AID,
            bridged,
            "hi.txt",
            2,
            HI_SHA,
            SystemTime::now(),
        )
        .unwrap();
        received_files::append_in(&received, AID, 0, "aA==").unwrap();
        assert_eq!(
            status_with(&data, Some(&received), bridged, AID),
            Ok(FileStatus::new("partial", 1))
        );
        received_files::append_in(&received, AID, 1, "aQ==").unwrap();
        let done = received_files::finish_in(&received, &downloads, AID).unwrap();
        assert_eq!(
            status_with(&data, Some(&received), bridged, AID),
            Ok(FileStatus::new("received", 2))
        );
        assert_eq!(
            target_with(&data, Some(&received), bridged, AID),
            Ok(PathBuf::from(done))
        );
        assert_eq!(
            status_with(&data, None, bridged, AID),
            Ok(FileStatus::new("none", 0))
        );
    }

    #[test]
    fn open_refuses_every_kind_outside_the_allowlist() {
        let tmp = TempDir::new("openable");
        let plain = "pdf png jpg jpeg gif webp bmp mp4 webm mov mkv mp3 wav m4a ogg flac aac \
                     txt md csv log json zip tar gz tgz 7z";
        let mut allowed: Vec<String> = plain.split_whitespace().map(|e| format!("a.{e}")).collect();
        // Lowercased, and taken after Windows drops trailing spaces and dots.
        for name in ["Report.PDF", "report.pdf. . ", "logs.tar.gz", ".pdf"] {
            allowed.push(name.into());
        }
        let programs = "setup.exe fix.js run.vbs setup.hta x.scr go.lnk site.url tool.py \
                        app-release.apk run.sh x.desktop page.html img.svg x.pdf.exe pdf \
                        a\u{202E}fdp.exe";
        let mut refused: Vec<String> = programs.split_whitespace().map(String::from).collect();
        for name in ["x.exe. .", "x.p df", "x.pdf\u{2007}"] {
            refused.push(name.into());
        }
        for (n, name) in allowed.iter().chain(refused.iter()).enumerate() {
            let aid = format!("{n:032x}");
            let dir = artifact_dir(&tmp.0, "sess-1", &aid);
            fs::write(dir.join(name), b"x").unwrap();
            let got = open_target_with(&tmp.0, None, "sess-1", &aid);
            if allowed.contains(name) {
                assert!(got.is_ok(), "{name:?} should open: {got:?}");
            } else {
                assert_eq!(got, Err("not_openable".into()), "{name:?}");
            }
        }
        // Lookup errors still come first.
        assert_eq!(
            open_target_with(&tmp.0, None, "sess-1", "nope"),
            Err("bad_id".into())
        );
        assert_eq!(
            open_target_with(&tmp.0, None, "sess-9", AID),
            Err("not_found".into())
        );
    }

    #[test]
    fn save_a_copy_never_truncates_the_source() {
        let tmp = TempDir::new("copy");
        let source = tmp.0.join("hi.txt");
        fs::write(&source, b"hi").unwrap();
        copy_to(&source, &source).unwrap();
        assert_eq!(fs::read(&source).unwrap(), b"hi");
        copy_to(&source, &tmp.0.join("copy.txt")).unwrap();
        assert_eq!(fs::read(tmp.0.join("copy.txt")).unwrap(), b"hi");
    }

    #[test]
    fn file_status_serializes_as_the_ui_reads_it() {
        let json = serde_json::to_value(FileStatus::new("partial", 7)).unwrap();
        assert_eq!(json, serde_json::json!({ "state": "partial", "have": 7 }));
    }
}
