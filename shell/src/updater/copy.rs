//! User-facing sentences for the updater.
//!
//! The host owns this copy. The window shows `detail` as given.

use super::PackageKind;

pub fn up_to_date_detail() -> &'static str {
    "You are on the latest published TST Desk release."
}

pub fn no_release_detail() -> &'static str {
    "No tstdesk-v* release is published yet."
}

pub fn unpackaged_detail(version: &str) -> String {
    format!(
        "TST Desk {version} is published. This build isn't a packaged app, so it won't replace itself. Open the releases page and check SHA256SUMS.txt."
    )
}

pub fn manual_detail(version: &str) -> String {
    format!("TST Desk {version} is published. Download it to install after a checksum check.")
}

pub fn downloading_detail(name: &str, pct: Option<u8>) -> String {
    match pct {
        Some(p) => {
            format!("Downloading {name} — {p}%. It is installed only if it matches SHA256SUMS.txt.")
        }
        None => format!("Downloading {name}. It is installed only if it matches SHA256SUMS.txt."),
    }
}

pub fn ready_detail(version: &str, kind: PackageKind, can_apply: bool) -> String {
    if !can_apply {
        return match kind {
            PackageKind::MacosDmg => format!(
                "TST Desk {version} matches SHA256SUMS.txt. Open the disk image and replace the app. GitHub builds are unsigned."
            ),
            PackageKind::Deb => format!(
                "TST Desk {version} matches SHA256SUMS.txt. Open the package to install it."
            ),
            PackageKind::AppImage => format!(
                "TST Desk {version} matches SHA256SUMS.txt. Open the AppImage to run it."
            ),
            PackageKind::Msi => format!(
                "TST Desk {version} matches SHA256SUMS.txt. Open the installer. Windows will ask for permission."
            ),
        };
    }
    match kind {
        PackageKind::MacosDmg => format!(
            "TST Desk {version} matches SHA256SUMS.txt. Restart to install it. GitHub builds are unsigned, so macOS may ask for Screen Recording and Accessibility again."
        ),
        PackageKind::AppImage => format!(
            "TST Desk {version} matches SHA256SUMS.txt. Restart to replace this AppImage and reopen it."
        ),
        PackageKind::Msi => format!(
            "TST Desk {version} matches SHA256SUMS.txt. Restart to run the installer. Windows will ask for permission."
        ),
        PackageKind::Deb => format!(
            "TST Desk {version} matches SHA256SUMS.txt. Open the package to install it."
        ),
    }
}

pub fn checksum_failed_detail() -> &'static str {
    "The download does not match SHA256SUMS.txt. Nothing was installed."
}
