//! Host-side log files under `{data_dir}/logs` (TD-5021).
//!
//! tstd's stderr used to go to null, so a crash before its own logger
//! was up left nothing behind. It now goes to a file the host opens
//! before the spawn: a file, not a pipe, so tstd can still write it
//! after the host is gone. The release host logs its own lifecycle to
//! `host.log` beside it, and never the port-file token. The stderr file
//! is tstd's raw stream (uncaught tracebacks, interpreter warnings),
//! which the daemon's redaction filter never sees, so it is owner-only.

use std::fs::{File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};
use std::process::Stdio;

use tauri::plugin::TauriPlugin;
use tauri::Wry;
use tauri_plugin_log::{RotationStrategy, Target, TargetKind};

/// Same leaf as `core/tstd/logging.log_directory`.
const LOGS_DIR: &str = "logs";
const TSTD_STDERR_LOG: &str = "tstd.stderr.log";
/// tauri-plugin-log appends `.log` to the stem.
const HOST_LOG_STEM: &str = "host";
/// Both files roll over past this size.
const ROTATE_BYTES: u64 = 5 * 1024 * 1024;

pub(crate) fn logs_dir(data_dir: &Path) -> PathBuf {
    data_dir.join(LOGS_DIR)
}

pub(crate) fn tstd_stderr_path(data_dir: &Path) -> PathBuf {
    logs_dir(data_dir).join(TSTD_STDERR_LOG)
}

/// Directory and file stem for the host log target.
pub(crate) fn host_log_folder(data_dir: &Path) -> (PathBuf, String) {
    (logs_dir(data_dir), HOST_LOG_STEM.to_string())
}

/// The host's logger. Release writes its lifecycle (spawn, attach,
/// crash, restart, quit) to `host.log`; it used to log nothing, so a
/// restart left no trace. Dev keeps the plugin's default targets, so a
/// `tauri dev` run does not write into the installed app's logs.
pub(crate) fn host_log_plugin(data_dir: &Path) -> TauriPlugin<Wry> {
    let builder = tauri_plugin_log::Builder::default().level(log::LevelFilter::Info);
    if cfg!(debug_assertions) {
        return builder.build();
    }
    let (path, stem) = host_log_folder(data_dir);
    builder
        .clear_targets()
        .target(Target::new(TargetKind::Folder {
            path,
            file_name: Some(stem),
        }))
        .max_file_size(u128::from(ROTATE_BYTES))
        // One dated archive beside host.log, like tstd.stderr.log.1.
        // KeepSome(n) keeps n archives besides the active file.
        .rotation_strategy(RotationStrategy::KeepSome(1))
        .build()
}

/// stderr for a tstd spawn. A log that cannot be opened must not stop
/// the daemon from starting, so that case falls back to null.
pub(crate) fn tstd_stderr(data_dir: &Path) -> Stdio {
    match open_tstd_stderr(data_dir) {
        Ok(file) => Stdio::from(file),
        Err(e) => {
            log::warn!("could not open the tstd stderr log; discarding it: {e}");
            Stdio::null()
        }
    }
}

fn open_tstd_stderr(data_dir: &Path) -> io::Result<File> {
    std::fs::create_dir_all(logs_dir(data_dir))?;
    open_rolled(&tstd_stderr_path(data_dir), ROTATE_BYTES)
}

/// A rollover that fails still appends: a lost traceback costs more
/// than a file past the limit.
fn open_rolled(path: &Path, limit: u64) -> io::Result<File> {
    if let Err(e) = rotate_if_over(path, limit) {
        log::warn!("could not roll over the tstd stderr log; appending: {e}");
    }
    open_append_private(path)
}

/// Keep one older file as `<name>.1`. Rotation runs at spawn, so a
/// daemon that is already writing keeps its open file.
fn rotate_if_over(path: &Path, limit: u64) -> io::Result<()> {
    match std::fs::metadata(path) {
        Ok(meta) if meta.len() > limit => {
            let mut rolled = path.as_os_str().to_owned();
            rolled.push(".1");
            std::fs::rename(path, PathBuf::from(rolled))
        }
        Ok(_) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

/// Append, owner-only. The mode is reapplied because `mode` only takes
/// effect when the file is created.
fn open_append_private(path: &Path) -> io::Result<File> {
    let mut opts = OpenOptions::new();
    opts.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let file = opts.open(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_data_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "tstd-logs-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn both_logs_resolve_under_data_dir_logs() {
        let data_dir = Path::new("/data/tst-desk");
        let logs = data_dir.join("logs");
        assert_eq!(tstd_stderr_path(data_dir), logs.join("tstd.stderr.log"));
        let (dir, stem) = host_log_folder(data_dir);
        assert_eq!(dir, logs);
        assert_eq!(dir.join(format!("{stem}.log")), logs.join("host.log"));
    }

    #[test]
    fn stderr_log_rolls_over_to_dot_one() {
        let data_dir = temp_data_dir("rotate");
        std::fs::create_dir_all(logs_dir(&data_dir)).unwrap();
        let path = tstd_stderr_path(&data_dir);
        std::fs::write(&path, vec![b'x'; 64]).unwrap();
        rotate_if_over(&path, 32).unwrap();
        let rolled = logs_dir(&data_dir).join("tstd.stderr.log.1");
        assert_eq!(std::fs::read(&rolled).unwrap().len(), 64);
        assert!(!path.exists());
        std::fs::write(&path, b"small").unwrap();
        rotate_if_over(&path, 32).unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"small");
        let _ = std::fs::remove_dir_all(&data_dir);
    }

    #[test]
    fn missing_log_is_not_a_rotation_error() {
        let data_dir = temp_data_dir("missing");
        assert!(rotate_if_over(&tstd_stderr_path(&data_dir), 1).is_ok());
        let _ = std::fs::remove_dir_all(&data_dir);
    }

    #[cfg(unix)]
    #[test]
    fn stderr_log_appends_and_is_owner_only() {
        use std::io::Write;
        use std::os::unix::fs::PermissionsExt;

        let data_dir = temp_data_dir("mode");
        std::fs::create_dir_all(logs_dir(&data_dir)).unwrap();
        let path = tstd_stderr_path(&data_dir);
        std::fs::write(&path, b"first\n").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        let mut file = open_tstd_stderr(&data_dir).unwrap();
        file.write_all(b"second\n").unwrap();
        drop(file);
        assert_eq!(std::fs::read(&path).unwrap(), b"first\nsecond\n");
        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
        let _ = std::fs::remove_dir_all(&data_dir);
    }

    #[cfg(unix)]
    #[test]
    fn failed_rollover_still_appends() {
        use std::io::Write;

        let data_dir = temp_data_dir("stuck");
        std::fs::create_dir_all(logs_dir(&data_dir)).unwrap();
        let path = tstd_stderr_path(&data_dir);
        std::fs::write(&path, vec![b'x'; 64]).unwrap();
        // A non-empty directory where `.1` should go makes the rename fail.
        let rolled = logs_dir(&data_dir).join("tstd.stderr.log.1");
        std::fs::create_dir_all(rolled.join("busy")).unwrap();
        assert!(rotate_if_over(&path, 32).is_err());
        let mut file = open_rolled(&path, 32).unwrap();
        file.write_all(b"more").unwrap();
        drop(file);
        let text = std::fs::read(&path).unwrap();
        assert_eq!(text.len(), 68);
        assert!(text.ends_with(b"more"));
        let _ = std::fs::remove_dir_all(&data_dir);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn spawned_daemon_stderr_lands_in_the_log() {
        let data_dir = temp_data_dir("spawn");
        let argv: Vec<String> = ["sh", "-c", "echo to-stdout; echo to-stderr >&2"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let mut child = super::super::spawn_daemon_with(&data_dir, &argv).unwrap();
        let status = child.wait().await.unwrap();
        assert!(status.success());
        let text = std::fs::read_to_string(tstd_stderr_path(&data_dir)).unwrap();
        assert_eq!(text, "to-stderr\n");
        let _ = std::fs::remove_dir_all(&data_dir);
    }
}
