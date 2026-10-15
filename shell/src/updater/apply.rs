//! Replace the running install after the process exits.
//!
//! The helper is a fixed script. Paths arrive as arguments, never
//! interpolated, and it waits out the host so it is not copying over a
//! binary that is still mapped. A `target/` build is a dev tree: it can
//! report a release, and it must not install over itself.

use std::path::{Path, PathBuf};
use std::process::Stdio;

use super::plan::PackageKind;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApplyTarget {
    pub packaged: bool,
    pub can_apply: bool,
    pub can_open_installer: bool,
    pub dest: Option<PathBuf>,
}

pub fn looks_like_dev_build(exe: &Path) -> bool {
    exe.components().any(|c| c.as_os_str() == "target")
}

pub fn macos_bundle_from_exe(exe: &Path) -> Option<PathBuf> {
    let macos = exe.parent()?;
    if macos.file_name()? != "MacOS" {
        return None;
    }
    let contents = macos.parent()?;
    if contents.file_name()? != "Contents" {
        return None;
    }
    let app = contents.parent()?;
    if app.extension()? != "app" {
        return None;
    }
    Some(app.to_path_buf())
}

pub fn dir_writable(dir: &Path) -> bool {
    let probe = dir.join(".tst-update-probe");
    match std::fs::write(&probe, b"") {
        Ok(()) => {
            let _ = std::fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

pub fn assess(kind: PackageKind, exe: &Path) -> ApplyTarget {
    if looks_like_dev_build(exe) {
        return ApplyTarget {
            packaged: false,
            can_apply: false,
            can_open_installer: false,
            dest: None,
        };
    }
    match kind {
        PackageKind::MacosDmg => match macos_bundle_from_exe(exe) {
            Some(bundle) => {
                let writable = bundle.parent().is_some_and(dir_writable);
                ApplyTarget {
                    packaged: true,
                    can_apply: writable,
                    can_open_installer: !writable,
                    dest: Some(bundle),
                }
            }
            None => unpackaged(),
        },
        PackageKind::AppImage => match std::env::var_os("APPIMAGE") {
            Some(raw) => {
                let dest = PathBuf::from(raw);
                let writable = dest.parent().is_some_and(dir_writable) && dest.is_file();
                ApplyTarget {
                    packaged: true,
                    can_apply: writable,
                    can_open_installer: !writable,
                    dest: Some(dest),
                }
            }
            None => ApplyTarget {
                packaged: true,
                can_apply: false,
                can_open_installer: true,
                dest: None,
            },
        },
        PackageKind::Msi => ApplyTarget {
            packaged: true,
            can_apply: true,
            can_open_installer: false,
            dest: None,
        },
        PackageKind::Deb => ApplyTarget {
            packaged: true,
            can_apply: false,
            can_open_installer: true,
            dest: None,
        },
    }
}

fn unpackaged() -> ApplyTarget {
    ApplyTarget {
        packaged: false,
        can_apply: false,
        can_open_installer: false,
        dest: None,
    }
}

pub fn current_exe() -> Result<PathBuf, String> {
    std::env::current_exe().map_err(|e| format!("could not locate this app: {e}"))
}

pub fn file_sha256(path: &Path) -> Result<String, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("could not read the download: {e}"))?;
    Ok(super::plan::sha256_hex(&bytes))
}

/// Copy `TST Desk.app` out of a disk image. The image itself was already
/// checksum-verified; this only unpacks it.
#[cfg(target_os = "macos")]
pub fn extract_macos_app(dmg: &Path, dest_dir: &Path) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dest_dir).map_err(|e| format!("could not stage the update: {e}"))?;
    let mount = dest_dir.join("mnt");
    let _ = std::fs::remove_dir_all(&mount);
    std::fs::create_dir_all(&mount).map_err(|e| format!("could not stage the update: {e}"))?;
    let attach = std::process::Command::new("/usr/bin/hdiutil")
        .args(["attach", "-nobrowse", "-readonly", "-mountpoint"])
        .arg(&mount)
        .arg(dmg)
        .output()
        .map_err(|e| format!("couldn't open the disk image: {e}"))?;
    if !attach.status.success() {
        let _ = std::fs::remove_dir_all(&mount);
        let err = String::from_utf8_lossy(&attach.stderr);
        return Err(format!("couldn't open the disk image: {}", err.trim()));
    }
    let result = (|| {
        let app = mount.join("TST Desk.app");
        if !app.is_dir() {
            return Err("the disk image has no TST Desk.app".into());
        }
        let staged = dest_dir.join("TST Desk.app");
        let _ = std::fs::remove_dir_all(&staged);
        let copy = std::process::Command::new("/usr/bin/ditto")
            .arg(&app)
            .arg(&staged)
            .output()
            .map_err(|e| format!("couldn't copy the app out of the disk image: {e}"))?;
        if !copy.status.success() {
            let err = String::from_utf8_lossy(&copy.stderr);
            return Err(format!(
                "couldn't copy the app out of the disk image: {}",
                err.trim()
            ));
        }
        Ok(staged)
    })();
    let _ = std::process::Command::new("/usr/bin/hdiutil")
        .args(["detach", "-force"])
        .arg(&mount)
        .output();
    let _ = std::fs::remove_dir_all(&mount);
    result
}

#[cfg(not(target_os = "macos"))]
pub fn extract_macos_app(_dmg: &Path, _dest_dir: &Path) -> Result<PathBuf, String> {
    Err("disk images are only installed on macOS".into())
}

const MACOS_HELPER: &str = r#"#!/bin/bash
# Args: pid, staged.app, dest.app. Paths are arguments so nothing here is expanded.
set -euo pipefail
pid="$1"
src="$2"
dest="$3"
i=0
while [ "$i" -lt 120 ]; do
  if ! kill -0 "$pid" 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 0.5
done
if kill -0 "$pid" 2>/dev/null; then
  echo "TST Desk did not exit" >&2
  exit 1
fi
parent="$(dirname "$dest")"
base="$(basename "$dest")"
old="$parent/.$base.previous"
rm -rf "$old"
mv "$dest" "$old"
if ditto "$src" "$dest"; then
  xattr -dr com.apple.quarantine "$dest" 2>/dev/null || true
  rm -rf "$old"
  open "$dest"
else
  mv "$old" "$dest" || true
  exit 1
fi
"#;

const APPIMAGE_HELPER: &str = r#"#!/bin/bash
set -euo pipefail
pid="$1"
src="$2"
dest="$3"
i=0
while [ "$i" -lt 120 ]; do
  if ! kill -0 "$pid" 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 0.5
done
if kill -0 "$pid" 2>/dev/null; then
  echo "TST Desk did not exit" >&2
  exit 1
fi
tmp="$dest.tst-new"
cp -f "$src" "$tmp"
chmod +x "$tmp"
mv -f "$tmp" "$dest"
nohup "$dest" >/dev/null 2>&1 &
exit 0
"#;

const MSI_HELPER: &str = r#"param(
  [Parameter(Mandatory = $true)][int]$WaitPid,
  [Parameter(Mandatory = $true)][string]$Msi
)
$deadline = (Get-Date).AddMinutes(2)
while (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) {
  if ((Get-Date) -gt $deadline) { exit 1 }
  Start-Sleep -Milliseconds 500
}
$proc = Start-Process -FilePath "$env:SystemRoot\System32\msiexec.exe" -ArgumentList @("/i", $Msi, "/passive", "/norestart") -Wait -PassThru
if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) { exit $proc.ExitCode }
$candidates = @(
  "$env:ProgramFiles\TST Desk\tst-desk.exe",
  "${env:ProgramFiles(x86)}\TST Desk\tst-desk.exe",
  "$env:LOCALAPPDATA\TST Desk\tst-desk.exe"
)
foreach ($exe in $candidates) {
  if (Test-Path -LiteralPath $exe) {
    Start-Process -FilePath $exe
    exit 0
  }
}
exit 0
"#;

fn write_script(dir: &Path, name: &str, body: &str) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("could not write the updater: {e}"))?;
    let path = dir.join(name);
    std::fs::write(&path, body).map_err(|e| format!("could not write the updater: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700));
    }
    Ok(path)
}

fn helper_log(dir: &Path) -> Result<std::fs::File, String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("could not write the updater log: {e}"))?;
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("apply.log"))
        .map_err(|e| format!("could not write the updater log: {e}"))
}

/// Start the helper in its own process group so quitting this app does
/// not take the copy with it.
pub fn spawn_helper(
    kind: PackageKind,
    updates_dir: &Path,
    src: &Path,
    dest: Option<&Path>,
) -> Result<(), String> {
    let pid = std::process::id().to_string();
    let log = helper_log(updates_dir)?;
    let err = log
        .try_clone()
        .map_err(|e| format!("could not write the updater log: {e}"))?;
    match kind {
        PackageKind::MacosDmg | PackageKind::AppImage => {
            let dest = dest.ok_or_else(|| "this install has no path to replace".to_string())?;
            let (name, body) = if kind == PackageKind::MacosDmg {
                ("apply-macos.sh", MACOS_HELPER)
            } else {
                ("apply-appimage.sh", APPIMAGE_HELPER)
            };
            let script = write_script(updates_dir, name, body)?;
            let mut cmd = std::process::Command::new("/bin/bash");
            cmd.arg(&script)
                .arg(&pid)
                .arg(src)
                .arg(dest)
                .stdin(Stdio::null())
                .stdout(log)
                .stderr(err);
            #[cfg(unix)]
            {
                use std::os::unix::process::CommandExt;
                cmd.process_group(0);
            }
            cmd.spawn()
                .map(|_| ())
                .map_err(|e| format!("could not start the installer: {e}"))
        }
        PackageKind::Msi => {
            let script = write_script(updates_dir, "apply-windows.ps1", MSI_HELPER)?;
            let mut cmd = std::process::Command::new("powershell");
            cmd.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
                .arg(&script)
                .arg("-WaitPid")
                .arg(&pid)
                .arg("-Msi")
                .arg(src)
                .stdin(Stdio::null())
                .stdout(log)
                .stderr(err);
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                const DETACHED: u32 = 0x0000_0008;
                const NEW_GROUP: u32 = 0x0000_0200;
                cmd.creation_flags(DETACHED | NEW_GROUP);
            }
            cmd.spawn()
                .map(|_| ())
                .map_err(|e| format!("could not start the installer: {e}"))
        }
        PackageKind::Deb => Err("a .deb opens in the system installer".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundle_path_is_the_app_not_the_binary() {
        let exe = Path::new("/Applications/TST Desk.app/Contents/MacOS/tst-desk");
        let bundle = macos_bundle_from_exe(exe).unwrap();
        assert_eq!(bundle, Path::new("/Applications/TST Desk.app"));
        assert!(macos_bundle_from_exe(Path::new("/usr/local/bin/tst-desk")).is_none());
    }

    #[test]
    fn a_target_tree_is_not_an_install() {
        let exe = Path::new("/src/TST-Desk/shell/target/release/tst-desk");
        assert!(looks_like_dev_build(exe));
        let target = assess(PackageKind::MacosDmg, exe);
        assert!(!target.packaged);
        assert!(!target.can_apply);
        let installed = Path::new("/Applications/TST Desk.app/Contents/MacOS/tst-desk");
        assert!(!looks_like_dev_build(installed));
    }
}
