//! First-run copy for close ≠ quit (TD-2903).
//!
//! After the first hide-on-close the host shows this once and stamps
//! `{user_data_dir}/close-is-not-quit.yaml`. Presence of the file means
//! the user has already seen it — do not nag.

use std::path::Path;

/// Event the webview listens for. Payload is `{title, body}`.
pub const CLOSE_HINT_EVENT: &str = "close-is-not-quit";

pub const CLOSE_HINT_TITLE: &str = "TST Desk is still running";
pub const CLOSE_HINT_BODY: &str =
    "Closing the window hides TST Desk; Quit TST Desk is what stops it.";

pub fn close_hint_path(data_dir: &Path) -> std::path::PathBuf {
    data_dir.join("close-is-not-quit.yaml")
}

/// True when the one-shot notice has already been shown.
pub fn close_hint_already_shown(data_dir: &Path) -> bool {
    close_hint_path(data_dir).is_file()
}

/// Persist the flag and return true on the first hide only.
///
/// A write failure returns false so a later close can try again. Once
/// the file exists, every later call is false.
pub fn take_first_close_hint(data_dir: &Path) -> bool {
    if close_hint_already_shown(data_dir) {
        return false;
    }
    if let Err(e) = std::fs::create_dir_all(data_dir) {
        log::warn!("could not create data dir for close-is-not-quit.yaml: {e}");
        return false;
    }
    if let Err(e) = std::fs::write(close_hint_path(data_dir), "shown: true\n") {
        log::warn!("could not persist close-is-not-quit.yaml: {e}");
        return false;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch() -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "tstd-close-hint-{}-{}",
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
    fn first_hide_is_once() {
        let dir = scratch();
        assert!(!close_hint_already_shown(&dir));
        assert!(take_first_close_hint(&dir));
        assert!(close_hint_already_shown(&dir));
        assert!(!take_first_close_hint(&dir));
        let text = std::fs::read_to_string(close_hint_path(&dir)).unwrap();
        assert!(text.contains("shown: true"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn copy_names_quit() {
        assert!(CLOSE_HINT_BODY.contains("Quit TST Desk"));
        assert!(CLOSE_HINT_BODY.contains("hides"));
    }
}
