//! Release selection for the updater (TD-4837).
//!
//! Tags, asset names, and download URLs are decided here so the network
//! code never invents a host. GitHub's `v*` tags belong to tst-cu-mcp
//! (TD-4812); only `tstdesk-v*` is this app.

use std::cmp::Ordering;
use std::collections::BTreeMap;
use std::path::Path;
use std::time::Duration;

use serde::Deserialize;

pub const GITHUB_REPO: &str = "ThatSimpleTech/TST-Desk";
pub const RELEASE_TAG_PREFIX: &str = "tstdesk-v";
pub const CHECK_INTERVAL: Duration = Duration::from_secs(4 * 60 * 60);
pub const STARTUP_DELAY: Duration = Duration::from_secs(8);
pub const MAX_DOWNLOAD_BYTES: u64 = 500 * 1024 * 1024;
pub const MAX_SUMS_BYTES: usize = 1024 * 1024;

const SUMS_NAME: &str = "SHA256SUMS.txt";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PackageKind {
    MacosDmg,
    AppImage,
    Msi,
    Deb,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReleaseAsset {
    pub name: String,
    pub url: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Release {
    pub version: String,
    pub html_url: String,
    pub assets: Vec<ReleaseAsset>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChosenAsset {
    pub name: String,
    pub url: String,
    pub kind: PackageKind,
    pub sums_url: String,
}

#[derive(Debug, Clone, Copy)]
pub struct Platform {
    pub os: &'static str,
    pub arch: &'static str,
    pub appimage: bool,
}

pub fn host_platform() -> Platform {
    Platform {
        os: std::env::consts::OS,
        arch: std::env::consts::ARCH,
        appimage: std::env::var_os("APPIMAGE").is_some(),
    }
}

/// Dotted numeric versions only. `0.1.0-rc1` is not a release tag.
pub fn parse_release_version(tag_name: &str) -> Option<&str> {
    let ver = tag_name.strip_prefix(RELEASE_TAG_PREFIX)?;
    let parts: Vec<&str> = ver.split('.').collect();
    if parts.len() < 2 {
        return None;
    }
    if parts
        .iter()
        .all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit()))
    {
        Some(ver)
    } else {
        None
    }
}

pub fn compare_versions(current: &str, latest: &str) -> Ordering {
    let cur: Vec<u32> = current.split('.').filter_map(|s| s.parse().ok()).collect();
    let lat: Vec<u32> = latest.split('.').filter_map(|s| s.parse().ok()).collect();
    for i in 0..cur.len().max(lat.len()) {
        let c = cur.get(i).copied().unwrap_or(0);
        let l = lat.get(i).copied().unwrap_or(0);
        match c.cmp(&l) {
            Ordering::Equal => {}
            other => return other,
        }
    }
    Ordering::Equal
}

#[derive(Deserialize)]
struct RawRelease {
    tag_name: String,
    html_url: String,
    draft: bool,
    prerelease: bool,
    assets: Vec<RawAsset>,
}

#[derive(Deserialize)]
struct RawAsset {
    name: String,
    browser_download_url: String,
}

pub fn parse_releases(body: &str) -> Result<Vec<Release>, String> {
    let value: serde_json::Value =
        serde_json::from_str(body).map_err(|e| format!("release JSON parse failed: {e}"))?;
    if let Some(msg) = value.get("message").and_then(|m| m.as_str()) {
        if !value.is_array() {
            return Err(format!("GitHub returned an error: {msg}"));
        }
    }
    let raw: Vec<RawRelease> =
        serde_json::from_value(value).map_err(|e| format!("release JSON parse failed: {e}"))?;
    let mut out = Vec::new();
    for rel in raw {
        if rel.draft || rel.prerelease {
            continue;
        }
        let Some(version) = parse_release_version(&rel.tag_name) else {
            continue;
        };
        if !release_page_allowed(&rel.html_url) {
            continue;
        }
        let assets = rel
            .assets
            .into_iter()
            .filter(|a| safe_name(&a.name))
            .map(|a| ReleaseAsset {
                name: a.name,
                url: a.browser_download_url,
            })
            .collect();
        out.push(Release {
            version: version.to_string(),
            html_url: rel.html_url,
            assets,
        });
    }
    Ok(out)
}

pub fn newest(releases: &[Release]) -> Option<&Release> {
    releases
        .iter()
        .max_by(|a, b| compare_versions(&a.version, &b.version))
}

fn preferred_names(version: &str, platform: &Platform) -> Vec<(String, PackageKind)> {
    match (platform.os, platform.arch, platform.appimage) {
        ("macos", "aarch64", _) => {
            vec![(
                format!("TST.Desk_{version}_aarch64.dmg"),
                PackageKind::MacosDmg,
            )]
        }
        ("macos", "x86_64", _) => {
            vec![(format!("TST.Desk_{version}_x64.dmg"), PackageKind::MacosDmg)]
        }
        ("linux", "x86_64", true) => {
            vec![(
                format!("TST.Desk_{version}_amd64.AppImage"),
                PackageKind::AppImage,
            )]
        }
        ("linux", "x86_64", false) => vec![
            (format!("TST.Desk_{version}_amd64.deb"), PackageKind::Deb),
            (
                format!("TST.Desk_{version}_amd64.AppImage"),
                PackageKind::AppImage,
            ),
        ],
        ("linux", "aarch64", true) => {
            vec![(
                format!("TST.Desk_{version}_aarch64.AppImage"),
                PackageKind::AppImage,
            )]
        }
        ("linux", "aarch64", false) => vec![
            (format!("TST.Desk_{version}_arm64.deb"), PackageKind::Deb),
            (
                format!("TST.Desk_{version}_aarch64.AppImage"),
                PackageKind::AppImage,
            ),
        ],
        ("windows", "x86_64", _) => {
            vec![(
                format!("TST.Desk_{version}_x64_en-US.msi"),
                PackageKind::Msi,
            )]
        }
        _ => Vec::new(),
    }
}

pub fn choose_asset(release: &Release, platform: &Platform) -> Option<ChosenAsset> {
    let sums = release
        .assets
        .iter()
        .find(|a| a.name == SUMS_NAME && initial_download_allowed(&a.url))?;
    for (name, kind) in preferred_names(&release.version, platform) {
        let Some(asset) = release.assets.iter().find(|a| a.name == name) else {
            continue;
        };
        if !initial_download_allowed(&asset.url) {
            continue;
        }
        return Some(ChosenAsset {
            name: asset.name.clone(),
            url: asset.url.clone(),
            kind,
            sums_url: sums.url.clone(),
        });
    }
    None
}

pub fn safe_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() < 200
        && !name.starts_with('.')
        && !name.contains("..")
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-')
}

fn https_default_port(url: &reqwest::Url) -> bool {
    url.scheme() == "https" && matches!(url.port(), None | Some(443))
}

pub fn releases_url() -> String {
    format!("https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30")
}

pub fn releases_api_allowed(url: &str) -> bool {
    let Ok(parsed) = reqwest::Url::parse(url) else {
        return false;
    };
    https_default_port(&parsed)
        && parsed.host_str() == Some("api.github.com")
        && parsed.path() == format!("/repos/{GITHUB_REPO}/releases")
        && parsed.query() == Some("per_page=30")
}

/// The URL we request first. Redirects are a separate allowlist — GitHub
/// sends the bytes from a CDN host, not from `github.com`.
pub fn initial_download_allowed(url: &str) -> bool {
    let Ok(parsed) = reqwest::Url::parse(url) else {
        return false;
    };
    if !https_default_port(&parsed) || parsed.host_str() != Some("github.com") {
        return false;
    }
    if parsed.query().is_some() || parsed.fragment().is_some() {
        return false;
    }
    let prefix = format!("/{GITHUB_REPO}/releases/download/");
    let Some(rest) = parsed.path().strip_prefix(&prefix) else {
        return false;
    };
    let mut parts = rest.split('/');
    let Some(tag) = parts.next() else {
        return false;
    };
    let Some(file) = parts.next() else {
        return false;
    };
    parts.next().is_none() && parse_release_version(tag).is_some() && safe_name(file)
}

pub fn release_page_allowed(url: &str) -> bool {
    let Ok(parsed) = reqwest::Url::parse(url) else {
        return false;
    };
    if !https_default_port(&parsed) || parsed.host_str() != Some("github.com") {
        return false;
    }
    if parsed.query().is_some() || parsed.fragment().is_some() {
        return false;
    }
    let prefix = format!("/{GITHUB_REPO}/releases/tag/{RELEASE_TAG_PREFIX}");
    parsed.path().starts_with(&prefix) && !parsed.path().contains("..")
}

/// Hosts a release download may be redirected to (TD-5204).
///
/// GitHub answers `releases/download/...` with a 302 to its asset store.
/// Since 2025 that is `release-assets.githubusercontent.com`; a list
/// without it refused every download with "error following redirect".
/// Check the live answer with `curl -sI <asset URL>` before changing this.
pub fn redirect_host_allowed(host: &str) -> bool {
    matches!(
        host,
        "github.com"
            | "api.github.com"
            | "release-assets.githubusercontent.com"
            | "objects.githubusercontent.com"
            | "github-releases.githubusercontent.com"
    )
}

/// A redirect is followed only over HTTPS on the default port, to an
/// allowed host: the checksum file travels the same way as the asset, so a
/// downgraded hop would let one party supply both.
pub fn redirect_allowed(url: &reqwest::Url) -> bool {
    https_default_port(url) && url.host_str().is_some_and(redirect_host_allowed)
}

pub fn parse_sha256sums(text: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut parts = line.split_whitespace();
        let Some(hash) = parts.next() else {
            continue;
        };
        let Some(name) = parts.next() else {
            continue;
        };
        if parts.next().is_some() {
            continue;
        }
        let name = name.trim_start_matches('*');
        if hash.len() != 64 || !hash.chars().all(|c| c.is_ascii_hexdigit()) || !safe_name(name) {
            continue;
        }
        out.insert(name.to_string(), hash.to_ascii_lowercase());
    }
    out
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(bytes))
}

#[path = "copy.rs"]
mod copy;

pub use copy::{
    checksum_failed_detail, downloading_detail, manual_detail, no_release_detail, ready_detail,
    unpackaged_detail, up_to_date_detail,
};

/// Absent or unreadable means on — the same default as Claude Desktop.
/// Only an explicit `automatic: false` turns the background check off.
pub fn automatic_from_yaml(text: &str) -> bool {
    let Ok(root) = serde_yml::from_str::<serde_yml::Value>(text) else {
        return true;
    };
    let Some(map) = root.as_mapping() else {
        return true;
    };
    match map.get("automatic") {
        Some(serde_yml::Value::Bool(on)) => *on,
        _ => true,
    }
}

pub fn automatic_yaml(on: bool) -> String {
    format!("automatic: {on}\n")
}

pub fn load_automatic(data_dir: &Path) -> bool {
    match std::fs::read_to_string(data_dir.join("update.yaml")) {
        Ok(text) => automatic_from_yaml(&text),
        Err(_) => true,
    }
}

pub fn store_automatic(data_dir: &Path, on: bool) -> Result<(), String> {
    std::fs::create_dir_all(data_dir).map_err(|e| format!("could not create the data dir: {e}"))?;
    std::fs::write(data_dir.join("update.yaml"), automatic_yaml(on))
        .map_err(|e| format!("could not save the update preference: {e}"))
}

#[cfg(test)]
#[path = "plan_test.rs"]
mod tests;
