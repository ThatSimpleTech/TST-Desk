//! What the window may hand to the OS opener (PKG-12, INSPECT-03).
//!
//! Every "open in the system editor" control goes through the host's
//! `open_path` command. The opener plugin's `allow-open-path` permission was
//! granted with no scope, which allows nothing, so every one of them did
//! nothing in a release build. Rather than a static glob scope (the paths sit
//! in whichever workspace the person picked), the host checks each path when
//! it is asked: it must exist, and it must not be something the OS opener
//! would run instead of show.

use std::path::{Path, PathBuf};

/// Extensions the OS opener runs, installs or hands to an interpreter.
const RUNNABLE: &[&str] = &[
    "desktop", "sh", "bash", "zsh", "fish", "command", "tool", "app", "appimage", "run", "bin",
    "exe", "com", "bat", "cmd", "ps1", "psm1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "msi",
    "msp", "scr", "lnk", "url", "jar", "deb", "rpm", "pkg", "dmg", "apk", "reg",
];

/// The canonical path to open, or why it is refused (a sentence for a toast).
pub fn openable(path: &str) -> Result<PathBuf, String> {
    let raw = Path::new(path.trim());
    if raw.as_os_str().is_empty() {
        return Err("No file was named.".into());
    }
    let real = raw
        .canonicalize()
        .map_err(|_| format!("{} is not there any more.", raw.display()))?;
    if real.is_dir() {
        return Ok(real);
    }
    let name = real
        .file_name()
        .map(|n| n.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    let runnable_ext = real
        .extension()
        .map(|e| e.to_string_lossy().to_lowercase())
        .is_some_and(|e| RUNNABLE.contains(&e.as_str()));
    if runnable_ext || executable(&real) {
        return Err(format!(
            "{name} is a program or launcher, so it is not opened from here. Open its folder instead."
        ));
    }
    Ok(real)
}

#[cfg(unix)]
fn executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn executable(_path: &Path) -> bool {
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn scratch(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("tst-open-check-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_document_and_a_folder_open() {
        let dir = scratch("docs");
        let file = dir.join("AGENTS.md");
        fs::write(&file, "# rules\n").unwrap();
        assert_eq!(
            openable(file.to_str().unwrap()).unwrap(),
            file.canonicalize().unwrap()
        );
        assert!(openable(dir.to_str().unwrap()).is_ok());
    }

    #[test]
    fn a_missing_path_is_refused_with_a_sentence() {
        let err = openable("/nonexistent/tst/nowhere.md").unwrap_err();
        assert!(err.contains("is not there any more"), "{err}");
        assert!(openable("  ").is_err());
    }

    #[test]
    fn launchers_and_scripts_are_refused() {
        let dir = scratch("run");
        for name in [
            "evil.desktop",
            "setup.sh",
            "Install.EXE",
            "run.bat",
            "x.appimage",
        ] {
            let file = dir.join(name);
            fs::write(&file, "x").unwrap();
            let err = openable(file.to_str().unwrap()).unwrap_err();
            assert!(err.contains("program or launcher"), "{name}: {err}");
        }
    }

    #[cfg(unix)]
    #[test]
    fn an_executable_file_is_refused_whatever_its_name() {
        use std::os::unix::fs::PermissionsExt;
        let dir = scratch("exec");
        let file = dir.join("notes.txt");
        fs::write(&file, "#!/bin/sh\n").unwrap();
        fs::set_permissions(&file, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(openable(file.to_str().unwrap()).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn a_link_is_judged_by_what_it_points_to() {
        let dir = scratch("link");
        let target = dir.join("launch.desktop");
        fs::write(&target, "[Desktop Entry]\n").unwrap();
        let link = dir.join("readme.md");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(openable(link.to_str().unwrap()).is_err());
    }
}
