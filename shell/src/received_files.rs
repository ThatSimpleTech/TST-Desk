//! Files the agent sent from a session on another computer (TD-5144,
//! design §7.2).
//!
//! The bytes arrive over the hub's WebSocket in chunks; the UI hands each
//! one here in order. They go to `<app_cache_dir>/received/<aid>.part`
//! beside a small meta file, and once the whole file is in and its sha256
//! matches, it moves to the user's Downloads folder under a safe, unique
//! name. Every command takes the artifact id, never a path: the webview
//! cannot name a file to write.
//!
//! The functions that touch the disk take their directories as arguments,
//! so the tests run them against temp dirs; the Tauri commands only work
//! out where those directories are.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use base64::Engine as _;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};

use crate::sent_files::{check_id, FileStatus};

/// The largest file a peer may send (§7.2, the same cap as the phone's).
const MAX_SIZE: u64 = 2 * 1024 * 1024 * 1024;
/// Parts and metas nobody came back for are swept after this long.
const SWEEP_AFTER: Duration = Duration::from_secs(7 * 24 * 60 * 60);
const HASH_BUF: usize = 1024 * 1024;
const MAX_NAME_BYTES: usize = 120;
const MAX_EXT_CHARS: usize = 16;

/// `<aid>.json` (§1.3).
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ReceivedMeta {
    pub session_id: String,
    pub name: String,
    pub size: u64,
    pub sha256: String,
    pub done_path: Option<String>,
    /// The copy's modified time once it was in Downloads, so another file
    /// later put at the same path is not taken for it. Absent in metas
    /// written before it existed; those trust the size alone.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub done_mtime_ns: Option<u64>,
}

/// `<app_cache_dir>/received/`. Not created here.
pub(crate) fn received_root(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_cache_dir()
        .map(|dir| dir.join("received"))
        .map_err(|e| format!("io: {e}"))
}

fn part_path(root: &Path, aid: &str) -> PathBuf {
    root.join(format!("{aid}.part"))
}

fn meta_path(root: &Path, aid: &str) -> PathBuf {
    root.join(format!("{aid}.json"))
}

fn tmp_meta_path(root: &Path, aid: &str) -> PathBuf {
    root.join(format!("{aid}.json.tmp"))
}

/// A write that ran out of room is `no_space`; anything else is `io`.
pub(crate) fn io_err(e: io::Error) -> String {
    if is_disk_full(&e) {
        "no_space".into()
    } else {
        format!("io: {e}")
    }
}

// Raw codes, not ErrorKind::StorageFull: that kind is newer than the
// crate's declared rust-version.
#[cfg(unix)]
fn is_disk_full(e: &io::Error) -> bool {
    e.raw_os_error() == Some(28) // ENOSPC on Linux, macOS and the BSDs
}

#[cfg(windows)]
fn is_disk_full(e: &io::Error) -> bool {
    matches!(e.raw_os_error(), Some(39) | Some(112)) // ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
}

#[cfg(not(any(unix, windows)))]
fn is_disk_full(_e: &io::Error) -> bool {
    false
}

fn ensure_root(root: &Path) -> Result<(), String> {
    fs::create_dir_all(root).map_err(io_err)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(root, fs::Permissions::from_mode(0o700)).map_err(io_err)?;
    }
    Ok(())
}

fn private_options() -> OpenOptions {
    let mut options = OpenOptions::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
}

fn read_meta(root: &Path, aid: &str) -> Option<ReceivedMeta> {
    let raw = fs::read(meta_path(root, aid)).ok()?;
    serde_json::from_slice(&raw).ok()
}

/// Write the meta beside the part, atomically.
fn write_meta(root: &Path, aid: &str, meta: &ReceivedMeta) -> Result<(), String> {
    let tmp = tmp_meta_path(root, aid);
    let body = serde_json::to_vec(meta).map_err(|e| format!("io: {e}"))?;
    let mut file = private_options()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&tmp)
        .map_err(io_err)?;
    file.write_all(&body).map_err(io_err)?;
    file.sync_all().map_err(io_err)?;
    drop(file);
    fs::rename(&tmp, meta_path(root, aid)).map_err(io_err)
}

fn part_len(root: &Path, aid: &str) -> Option<u64> {
    fs::symlink_metadata(part_path(root, aid))
        .ok()
        .filter(|m| m.file_type().is_file())
        .map(|m| m.len())
}

fn mtime_ns(meta: &fs::Metadata) -> Option<u64> {
    let since = meta
        .modified()
        .ok()?
        .duration_since(SystemTime::UNIX_EPOCH)
        .ok()?;
    u64::try_from(since.as_nanos()).ok()
}

/// The finished copy, if what is at its path is still that file: a regular
/// file of the sent size, last modified when it was moved there.
fn done_file(meta: &ReceivedMeta) -> Option<PathBuf> {
    let path = PathBuf::from(meta.done_path.as_ref()?);
    let found = fs::symlink_metadata(&path).ok()?;
    let same = found.file_type().is_file()
        && found.len() == meta.size
        && (meta.done_mtime_ns.is_none() || mtime_ns(&found) == meta.done_mtime_ns);
    same.then_some(path)
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// What this computer holds of a received file: `received` (in Downloads),
/// `partial` or `none`.
pub(crate) fn status_in(root: &Path, aid: &str) -> FileStatus {
    let Some(meta) = read_meta(root, aid) else {
        return FileStatus::new("none", 0);
    };
    if done_file(&meta).is_some() {
        return FileStatus::new("received", meta.size);
    }
    match part_len(root, aid) {
        Some(have) if have > 0 => FileStatus::new("partial", have),
        _ => FileStatus::new("none", 0),
    }
}

/// The path a finished download moved to, if it is still there.
pub(crate) fn done_path_in(root: &Path, aid: &str) -> Option<PathBuf> {
    read_meta(root, aid).as_ref().and_then(done_file)
}

/// Start or resume. The same size and sha256 carry on from the part (or
/// report the file already received); anything else starts over.
pub(crate) fn begin_in(
    root: &Path,
    aid: &str,
    session_id: &str,
    name: &str,
    size: u64,
    sha256: &str,
    now: SystemTime,
) -> Result<FileStatus, String> {
    check_id(aid)?;
    if size > MAX_SIZE {
        return Err("too_large".into());
    }
    // The sha256 names the content, so a malformed one is a bad id too.
    if !is_sha256(sha256) {
        return Err("bad_id".into());
    }
    ensure_root(root)?;
    sweep(root, aid, now);

    let wanted = ReceivedMeta {
        session_id: session_id.to_string(),
        name: name.to_string(),
        size,
        sha256: sha256.to_string(),
        done_path: None,
        done_mtime_ns: None,
    };
    let part = part_path(root, aid);
    match read_meta(root, aid) {
        Some(meta) if meta.size == size && meta.sha256 == sha256 => {
            if done_file(&meta).is_some() {
                return Ok(FileStatus::new("received", size));
            }
            if meta.session_id != session_id || meta.name != name || meta.done_path.is_some() {
                write_meta(root, aid, &wanted)?;
            }
            // No truncate: this is the resume.
            private_options()
                .append(true)
                .create(true)
                .open(&part)
                .map_err(io_err)?;
            let have = part_len(root, aid).unwrap_or(0);
            if have > size {
                File::create(&part).map_err(io_err)?;
                return Ok(FileStatus::new("none", 0));
            }
            Ok(FileStatus::new(
                if have > 0 { "partial" } else { "none" },
                have,
            ))
        }
        _ => {
            private_options()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&part)
                .map_err(io_err)?;
            write_meta(root, aid, &wanted)?;
            Ok(FileStatus::new("none", 0))
        }
    }
}

/// Add one chunk at the end of the part. Returns the new length.
pub(crate) fn append_in(
    root: &Path,
    aid: &str,
    offset: u64,
    data_b64: &str,
) -> Result<u64, String> {
    check_id(aid)?;
    let Some(meta) = read_meta(root, aid) else {
        return Err("io: no download in progress".into());
    };
    let Some(have) = part_len(root, aid) else {
        return Err("io: no download in progress".into());
    };
    if offset != have {
        return Err(format!("bad_offset:{have}"));
    }
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(data_b64)
        .map_err(|e| format!("io: bad base64: {e}"))?;
    let end = have + bytes.len() as u64;
    if end > meta.size {
        return Err("io: more bytes than the file's size".into());
    }
    let mut file = OpenOptions::new()
        .append(true)
        .open(part_path(root, aid))
        .map_err(io_err)?;
    file.write_all(&bytes).map_err(io_err)?;
    Ok(end)
}

fn hash_file(path: &Path) -> io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; HASH_BUF];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

/// Check the whole part and move it to `downloads`. Returns the final path.
pub(crate) fn finish_in(root: &Path, downloads: &Path, aid: &str) -> Result<String, String> {
    check_id(aid)?;
    let Some(mut meta) = read_meta(root, aid) else {
        return Err("not_found".into());
    };
    if let Some(done) = done_file(&meta) {
        return Ok(done.to_string_lossy().into_owned());
    }
    let part = part_path(root, aid);
    let Some(have) = part_len(root, aid) else {
        return Err("not_found".into());
    };
    if have != meta.size {
        return Err("size_mismatch".into());
    }
    if hash_file(&part).map_err(io_err)? != meta.sha256 {
        let _ = fs::remove_file(&part);
        return Err("hash_mismatch".into());
    }
    fs::create_dir_all(downloads).map_err(io_err)?;
    let target = move_unique(&part, downloads, &safe_name(&meta.name))?;
    let shown = target.to_string_lossy().into_owned();
    meta.done_path = Some(shown.clone());
    meta.done_mtime_ns = fs::metadata(&target).ok().as_ref().and_then(mtime_ns);
    write_meta(root, aid, &meta)?;
    Ok(shown)
}

/// Move `from` into `dir` as `name`, or `stem (1).ext`, `stem (2).ext`, ...
/// The name is claimed with create_new first, so two finishes never pick
/// the same one and nothing the user has is overwritten.
fn move_unique(from: &Path, dir: &Path, name: &str) -> Result<PathBuf, String> {
    let (stem, ext) = split_extension(name);
    for n in 0..10_000u32 {
        let candidate = if n == 0 {
            name.to_string()
        } else {
            format!("{stem} ({n}){ext}")
        };
        let target = dir.join(candidate);
        match private_options().write(true).create_new(true).open(&target) {
            Ok(reserved) => {
                drop(reserved);
                if fs::rename(from, &target).is_ok() {
                    return Ok(target);
                }
                // Another filesystem: copy, then drop the part.
                return match fs::copy(from, &target) {
                    Ok(_) => {
                        let _ = fs::remove_file(from);
                        Ok(target)
                    }
                    Err(e) => {
                        let _ = fs::remove_file(&target);
                        Err(io_err(e))
                    }
                };
            }
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(io_err(e)),
        }
    }
    Err("io: no free name in the Downloads folder".into())
}

/// Throw the part and the meta away. The user's copy in Downloads stays.
pub(crate) fn discard_in(root: &Path, aid: &str) -> Result<(), String> {
    check_id(aid)?;
    for path in [
        part_path(root, aid),
        meta_path(root, aid),
        tmp_meta_path(root, aid),
    ] {
        match fs::remove_file(&path) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(io_err(e)),
        }
    }
    Ok(())
}

/// Remove every part and meta last touched more than 7 days ago, judged by
/// the newer of the pair. `keep` is the download being begun.
fn sweep(root: &Path, keep: &str, now: SystemTime) {
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    let mut newest: HashMap<String, (SystemTime, Vec<PathBuf>)> = HashMap::new();
    for entry in entries.flatten() {
        let file_name = entry.file_name();
        let Some(name) = file_name.to_str() else {
            continue;
        };
        let Some(aid) = name
            .strip_suffix(".part")
            .or_else(|| name.strip_suffix(".json"))
            .or_else(|| name.strip_suffix(".json.tmp"))
        else {
            continue;
        };
        if aid == keep || check_id(aid).is_err() {
            continue;
        }
        let Ok(modified) = entry.metadata().and_then(|m| m.modified()) else {
            continue;
        };
        let slot = newest
            .entry(aid.to_string())
            .or_insert((SystemTime::UNIX_EPOCH, Vec::new()));
        slot.0 = slot.0.max(modified);
        slot.1.push(entry.path());
    }
    for (when, paths) in newest.into_values() {
        if now.duration_since(when).is_ok_and(|age| age > SWEEP_AFTER) {
            for path in paths {
                let _ = fs::remove_file(path);
            }
        }
    }
}

// ── File names (§1.4; identical in Python, Rust and Kotlin) ────────────

/// The text from the last `.`, when that dot is not the first character and
/// the extension is at most 16 characters, dot included.
fn extension(name: &str) -> Option<usize> {
    let at = name.rfind('.')?;
    if at == 0 || name[at..].chars().count() > MAX_EXT_CHARS {
        return None;
    }
    Some(at)
}

fn split_extension(name: &str) -> (&str, &str) {
    match extension(name) {
        Some(at) => (&name[..at], &name[at..]),
        None => (name, ""),
    }
}

fn strip_trailing(name: &str) -> &str {
    name.trim_end_matches([' ', '.'])
}

pub(crate) fn is_device_name(name: &str) -> bool {
    let head = name.split('.').next().unwrap_or("").trim_end_matches(' ');
    let upper = head.to_ascii_uppercase();
    match upper.as_str() {
        "CON" | "PRN" | "AUX" | "NUL" => true,
        _ => {
            let bytes = upper.as_bytes();
            bytes.len() == 4
                && (upper.starts_with("COM") || upper.starts_with("LPT"))
                && (b'1'..=b'9').contains(&bytes[3])
        }
    }
}

fn device_prefix(name: String) -> String {
    if is_device_name(&name) {
        format!("_{name}")
    } else {
        name
    }
}

/// Cut to `max` UTF-8 bytes at a char boundary.
fn cut(text: &str, max: usize) -> &str {
    if text.len() <= max {
        return text;
    }
    let mut end = max;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    &text[..end]
}

fn truncate(name: String) -> String {
    if name.len() <= MAX_NAME_BYTES {
        return name;
    }
    match extension(&name) {
        Some(at) => {
            let ext = &name[at..];
            format!("{}{ext}", cut(&name[..at], MAX_NAME_BYTES - ext.len()))
        }
        None => cut(&name, MAX_NAME_BYTES).to_string(),
    }
}

/// Step 2: C0 controls and DEL, and the bidi controls that let a name such
/// as `invoice\u{202E}fdp.exe` show as a PDF in a file manager.
fn is_dropped(c: char) -> bool {
    matches!(
        c as u32,
        0x00..=0x1F | 0x7F | 0x200E | 0x200F | 0x202A..=0x202E | 0x2066..=0x2069
    )
}

/// A file name safe on every desktop OS, from whatever the peer sent.
pub(crate) fn safe_name(raw: &str) -> String {
    let base = raw.rsplit(['/', '\\']).next().unwrap_or("");
    let cleaned: String = base
        .chars()
        .filter(|c| !is_dropped(*c))
        .map(|c| match c {
            ':' | '*' | '?' | '"' | '<' | '>' | '|' => '_',
            other => other,
        })
        .collect();
    let name = device_prefix(strip_trailing(&cleaned).to_string());
    let name = truncate(name);
    // Step 8: a "_" added here can push the name one byte over, so cut once more.
    let name = truncate(device_prefix(strip_trailing(&name).to_string()));
    if name.is_empty() {
        "file".into()
    } else {
        name
    }
}

// ── Commands (§1.6) ────────────────────────────────────────────────────

#[tauri::command(async)]
pub fn received_file_begin(
    app: AppHandle,
    artifact_id: String,
    session_id: String,
    name: String,
    size: u64,
    sha256: String,
) -> Result<FileStatus, String> {
    let root = received_root(&app)?;
    begin_in(
        &root,
        &artifact_id,
        &session_id,
        &name,
        size,
        &sha256,
        SystemTime::now(),
    )
}

#[tauri::command(async)]
pub fn received_file_append(
    app: AppHandle,
    artifact_id: String,
    offset: u64,
    data_b64: String,
) -> Result<u64, String> {
    let root = received_root(&app)?;
    append_in(&root, &artifact_id, offset, &data_b64)
}

#[tauri::command]
pub async fn received_file_finish(app: AppHandle, artifact_id: String) -> Result<String, String> {
    let root = received_root(&app)?;
    let downloads = dirs::download_dir().ok_or_else(|| "io: no Downloads folder".to_string())?;
    tauri::async_runtime::spawn_blocking(move || finish_in(&root, &downloads, &artifact_id))
        .await
        .map_err(|e| format!("io: {e}"))?
}

#[tauri::command(async)]
pub fn received_file_discard(app: AppHandle, artifact_id: String) -> Result<(), String> {
    let root = received_root(&app)?;
    discard_in(&root, &artifact_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    const AID: &str = "0123456789abcdef0123456789abcdef";
    const HI_SHA: &str = "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4";

    /// A fresh dir under the system temp dir, removed on drop.
    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            let dir =
                std::env::temp_dir().join(format!("tst-received-{}-{tag}", std::process::id()));
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

    fn b64(bytes: &[u8]) -> String {
        base64::engine::general_purpose::STANDARD.encode(bytes)
    }

    fn begin(root: &Path, name: &str, size: u64, sha: &str) -> Result<FileStatus, String> {
        begin_in(
            root,
            AID,
            "sess-1:mch_1a2b3c",
            name,
            size,
            sha,
            SystemTime::now(),
        )
    }

    #[test]
    fn received_begin_resumes_and_resets_on_meta_change() {
        let tmp = TempDir::new("begin");
        let root = tmp.0.join("received");
        assert_eq!(
            begin(&root, "hi.txt", 2, HI_SHA),
            Ok(FileStatus::new("none", 0))
        );
        assert_eq!(append_in(&root, AID, 0, &b64(b"h")), Ok(1));
        assert_eq!(
            begin(&root, "hi.txt", 2, HI_SHA),
            Ok(FileStatus::new("partial", 1))
        );
        assert_eq!(status_in(&root, AID), FileStatus::new("partial", 1));

        // Another file under the same id (it changed): start over.
        let other = "1".repeat(64);
        assert_eq!(
            begin(&root, "hi.txt", 2, &other),
            Ok(FileStatus::new("none", 0))
        );
        assert_eq!(part_len(&root, AID), Some(0));
        assert_eq!(read_meta(&root, AID).unwrap().sha256, other);

        assert_eq!(
            begin_in(&root, "../x", "s", "a", 1, HI_SHA, SystemTime::now()),
            Err("bad_id".into())
        );
        assert_eq!(
            begin(&root, "big.bin", MAX_SIZE + 1, HI_SHA),
            Err("too_large".into())
        );
        assert_eq!(begin(&root, "hi.txt", 2, "abc"), Err("bad_id".into()));
    }

    #[cfg(unix)]
    #[test]
    fn received_dir_and_files_are_private() {
        use std::os::unix::fs::PermissionsExt;
        let tmp = TempDir::new("perms");
        let root = tmp.0.join("received");
        begin(&root, "hi.txt", 2, HI_SHA).unwrap();
        let mode = |p: &Path| fs::metadata(p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode(&root), 0o700);
        assert_eq!(mode(&part_path(&root, AID)), 0o600);
        assert_eq!(mode(&meta_path(&root, AID)), 0o600);
    }

    #[test]
    fn received_append_refuses_wrong_offset_and_past_size() {
        let tmp = TempDir::new("append");
        let root = tmp.0.join("received");
        assert!(append_in(&root, AID, 0, &b64(b"h"))
            .unwrap_err()
            .starts_with("io:"));
        begin(&root, "hi.txt", 2, HI_SHA).unwrap();
        assert_eq!(
            append_in(&root, AID, 1, &b64(b"h")),
            Err("bad_offset:0".into())
        );
        assert_eq!(append_in(&root, AID, 0, &b64(b"h")), Ok(1));
        assert_eq!(
            append_in(&root, AID, 0, &b64(b"h")),
            Err("bad_offset:1".into())
        );
        assert!(append_in(&root, AID, 1, &b64(b"ii"))
            .unwrap_err()
            .starts_with("io:"));
        assert!(append_in(&root, AID, 1, "@@")
            .unwrap_err()
            .starts_with("io:"));
        assert_eq!(part_len(&root, AID), Some(1));
        assert_eq!(
            append_in(&root, "nope", 1, &b64(b"i")),
            Err("bad_id".into())
        );
    }

    #[test]
    fn received_finish_checks_sha_and_moves_with_unique_name() {
        let tmp = TempDir::new("finish");
        let root = tmp.0.join("received");
        let downloads = tmp.0.join("Downloads");
        fs::create_dir_all(&downloads).unwrap();
        fs::write(downloads.join("hi.txt"), b"the user's own").unwrap();

        begin(&root, "out/hi.txt", 2, HI_SHA).unwrap();
        append_in(&root, AID, 0, &b64(b"h")).unwrap();
        assert_eq!(
            finish_in(&root, &downloads, AID),
            Err("size_mismatch".into())
        );
        append_in(&root, AID, 1, &b64(b"i")).unwrap();

        let done = PathBuf::from(finish_in(&root, &downloads, AID).unwrap());
        assert_eq!(done, downloads.join("hi (1).txt"));
        assert_eq!(fs::read(&done).unwrap(), b"hi");
        assert_eq!(
            fs::read(downloads.join("hi.txt")).unwrap(),
            b"the user's own"
        );
        assert!(!part_path(&root, AID).exists());
        assert_eq!(status_in(&root, AID), FileStatus::new("received", 2));
        assert_eq!(done_path_in(&root, AID), Some(done.clone()));
        assert_eq!(
            begin(&root, "hi.txt", 2, HI_SHA),
            Ok(FileStatus::new("received", 2))
        );
        // A second finish names the same file.
        assert_eq!(
            finish_in(&root, &downloads, AID),
            Ok(done.to_string_lossy().into_owned())
        );

        // The next file with that name takes the next number.
        let second = "fedcba9876543210fedcba9876543210";
        begin_in(&root, second, "s", "hi.txt", 2, HI_SHA, SystemTime::now()).unwrap();
        append_in(&root, second, 0, &b64(b"hi")).unwrap();
        assert_eq!(
            finish_in(&root, &downloads, second).map(PathBuf::from),
            Ok(downloads.join("hi (2).txt"))
        );

        // Deleted from Downloads: the card offers the download again.
        fs::remove_file(&done).unwrap();
        assert_eq!(status_in(&root, AID), FileStatus::new("none", 0));
        assert_eq!(
            finish_in(&root, &downloads, "a".repeat(32).as_str()),
            Err("not_found".into())
        );
    }

    #[test]
    fn received_copy_replaced_at_its_path_is_not_received() {
        let tmp = TempDir::new("replaced");
        let root = tmp.0.join("received");
        let downloads = tmp.0.join("Downloads");
        let receive = || {
            begin(&root, "app-release.apk", 2, HI_SHA).unwrap();
            append_in(&root, AID, 0, &b64(b"hi")).unwrap();
            PathBuf::from(finish_in(&root, &downloads, AID).unwrap())
        };
        let done = receive();
        assert_eq!(status_in(&root, AID), FileStatus::new("received", 2));

        // Moved away, and another build lands at the freed name.
        for other in [&b"new build"[..], b"ho"] {
            fs::remove_file(&done).unwrap();
            fs::write(&done, other).unwrap();
            let file = File::options().write(true).open(&done).unwrap();
            file.set_modified(SystemTime::now() - Duration::from_secs(3600))
                .unwrap();
            drop(file);
            assert_eq!(
                status_in(&root, AID),
                FileStatus::new("none", 0),
                "{other:?}"
            );
            assert_eq!(done_path_in(&root, AID), None);
            // This card fetches its own bytes again, beside the other file.
            assert_eq!(
                begin(&root, "app-release.apk", 2, HI_SHA),
                Ok(FileStatus::new("none", 0))
            );
            append_in(&root, AID, 0, &b64(b"hi")).unwrap();
            let again = PathBuf::from(finish_in(&root, &downloads, AID).unwrap());
            assert_ne!(again, done);
            assert_eq!(fs::read(&again).unwrap(), b"hi");
            assert_eq!(status_in(&root, AID), FileStatus::new("received", 2));
            fs::remove_file(again).unwrap();
        }

        // A meta from before the stamp still trusts a file of the right size.
        let mut meta = read_meta(&root, AID).unwrap();
        fs::write(&done, b"hi").unwrap();
        meta.done_path = Some(done.to_string_lossy().into_owned());
        let mut old = serde_json::to_value(&meta).unwrap();
        old.as_object_mut().unwrap().remove("doneMtimeNs");
        fs::write(meta_path(&root, AID), old.to_string()).unwrap();
        assert_eq!(status_in(&root, AID), FileStatus::new("received", 2));
        fs::write(&done, b"three").unwrap();
        assert_eq!(status_in(&root, AID), FileStatus::new("none", 0));
    }

    #[test]
    fn received_finish_hash_mismatch_deletes_part() {
        let tmp = TempDir::new("mismatch");
        let root = tmp.0.join("received");
        let downloads = tmp.0.join("Downloads");
        begin(&root, "hi.txt", 2, HI_SHA).unwrap();
        append_in(&root, AID, 0, &b64(b"ho")).unwrap();
        assert_eq!(
            finish_in(&root, &downloads, AID),
            Err("hash_mismatch".into())
        );
        assert!(!part_path(&root, AID).exists());
        assert!(!downloads.join("hi.txt").exists());
        // begin() starts a fresh part for the retry.
        assert_eq!(
            begin(&root, "hi.txt", 2, HI_SHA),
            Ok(FileStatus::new("none", 0))
        );
        assert_eq!(append_in(&root, AID, 0, &b64(b"hi")), Ok(2));
    }

    #[test]
    fn discard_and_sweep_remove_parts_and_metas() {
        let tmp = TempDir::new("sweep");
        let root = tmp.0.join("received");
        begin(&root, "hi.txt", 2, HI_SHA).unwrap();
        append_in(&root, AID, 0, &b64(b"h")).unwrap();
        let later = SystemTime::now() + SWEEP_AFTER + Duration::from_secs(60);
        let other = "fedcba9876543210fedcba9876543210";
        // A week on, beginning another file sweeps this one.
        begin_in(&root, other, "s", "x.bin", 1, HI_SHA, later).unwrap();
        assert!(!part_path(&root, AID).exists());
        assert!(!meta_path(&root, AID).exists());
        assert!(part_path(&root, other).exists());

        assert_eq!(discard_in(&root, other), Ok(()));
        assert!(!part_path(&root, other).exists());
        assert!(!meta_path(&root, other).exists());
        assert_eq!(discard_in(&root, other), Ok(()));
        assert_eq!(discard_in(&root, "../etc"), Err("bad_id".into()));
    }

    #[test]
    fn safe_name_vectors() {
        let cases: Vec<(String, String)> = vec![
            ("app-release.apk".into(), "app-release.apk".into()),
            (
                "android/app/build/outputs/apk/release/app-release.apk".into(),
                "app-release.apk".into(),
            ),
            ("C:\\out\\App.apk".into(), "App.apk".into()),
            ("a:b*c?d\"e<f>g|h.txt".into(), "a_b_c_d_e_f_g_h.txt".into()),
            ("report.pdf. . ".into(), "report.pdf".into()),
            ("CON".into(), "_CON".into()),
            ("con.tar.gz".into(), "_con.tar.gz".into()),
            ("COM1 .log".into(), "_COM1 .log".into()),
            ("CONSOLE.txt".into(), "CONSOLE.txt".into()),
            ("..".into(), "file".into()),
            ("".into(), "file".into()),
            ("\u{0007}bell\u{007f}.txt".into(), "bell.txt".into()),
            (".env".into(), ".env".into()),
            (
                format!("{}.apk", "x".repeat(200)),
                format!("{}.apk", "x".repeat(116)),
            ),
            ("é".repeat(100), "é".repeat(60)),
            (
                format!("{}.verylongextension12", "a".repeat(130)),
                "a".repeat(120),
            ),
            ("name with spaces.zip".into(), "name with spaces.zip".into()),
            ("a\u{202E}fdp.exe".into(), "afdp.exe".into()),
            (
                format!("CON{}y.log", " ".repeat(150)),
                format!("_CON{}.log", " ".repeat(112)),
            ),
        ];
        for (input, want) in &cases {
            let got = safe_name(input);
            assert_eq!(&got, want, "{input:?}");
            assert_eq!(safe_name(&got), got, "not idempotent for {input:?}");
            assert!(got.len() <= MAX_NAME_BYTES);
        }
    }

    #[test]
    fn safe_name_drops_every_bidi_control() {
        let bidi = [0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E]
            .into_iter()
            .chain(0x2066..=0x2069);
        for code in bidi {
            let mark = char::from_u32(code).unwrap();
            assert_eq!(
                safe_name(&format!("in{mark}voice.pdf")),
                "invoice.pdf",
                "{code:X}"
            );
        }
        // Neighbours of the ranges are ordinary text and stay.
        for kept in ['\u{200D}', '\u{2029}', '\u{202F}', '\u{2065}', '\u{206A}'] {
            assert_eq!(safe_name(&format!("a{kept}b")), format!("a{kept}b"));
        }
    }

    #[test]
    fn unique_names_keep_the_extension() {
        assert_eq!(split_extension("hi.txt"), ("hi", ".txt"));
        assert_eq!(split_extension("logs.tar.gz"), ("logs.tar", ".gz"));
        assert_eq!(split_extension(".env"), (".env", ""));
        assert_eq!(split_extension("noext"), ("noext", ""));
    }
}
