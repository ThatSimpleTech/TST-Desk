//! Opt-in release check against GitHub (TD-4704).
//!
//! User-initiated only from Settings → About. No background telemetry.
//! In-app install stays off until signed releases exist (TD-4903).

use std::cmp::Ordering;

use serde::Serialize;

const GITHUB_REPO: &str = "ThatSimpleTech/TST-Desk";
const RELEASE_TAG_PREFIX: &str = "tstdesk-v";

/// Tauri updater install path — off until signing ships (TD-4704 AC3).
pub const IN_APP_INSTALL_ENABLED: bool = false;

#[derive(Debug, Clone, Serialize)]
pub struct UpdateCheckResult {
    pub current: String,
    pub latest: Option<String>,
    pub update_available: bool,
    pub in_app_install_enabled: bool,
    pub release_url: Option<String>,
    pub detail: String,
}

#[derive(serde::Deserialize)]
struct GhRelease {
    tag_name: String,
    html_url: String,
    draft: bool,
    prerelease: bool,
}

/// Compare dotted numeric semver segments (no prerelease handling needed for tags).
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

pub fn parse_release_version(tag_name: &str) -> Option<&str> {
    tag_name.strip_prefix(RELEASE_TAG_PREFIX)
}

fn detail_for_result(update_available: bool, in_app: bool) -> String {
    if !update_available {
        return "You are on the latest published TST Desk release.".into();
    }
    if in_app {
        return "A newer signed release is available. You can install it from here.".into();
    }
    "A newer release is published. v0.1 ships unsigned — download from the releases page and verify SHA256SUMS.txt. In-app install turns on once signing lands.".into()
}

fn check_releases(current: &str, releases: &[GhRelease]) -> UpdateCheckResult {
    let mut best: Option<(&str, &str)> = None;
    for rel in releases {
        if rel.draft || rel.prerelease {
            continue;
        }
        let Some(ver) = parse_release_version(&rel.tag_name) else {
            continue;
        };
        let take = match best {
            None => true,
            Some((b, _)) => compare_versions(b, ver) == Ordering::Less,
        };
        if take {
            best = Some((ver, rel.html_url.as_str()));
        }
    }
    let in_app = IN_APP_INSTALL_ENABLED;
    match best {
        None => UpdateCheckResult {
            current: current.into(),
            latest: None,
            update_available: false,
            in_app_install_enabled: in_app,
            release_url: None,
            detail: "No tstdesk-v* release is published yet.".into(),
        },
        Some((latest, url)) if compare_versions(current, latest) == Ordering::Less => {
            UpdateCheckResult {
                current: current.into(),
                latest: Some(latest.into()),
                update_available: true,
                in_app_install_enabled: in_app,
                release_url: Some(url.into()),
                detail: detail_for_result(true, in_app),
            }
        }
        Some((latest, url)) => UpdateCheckResult {
            current: current.into(),
            latest: Some(latest.into()),
            update_available: false,
            in_app_install_enabled: in_app,
            release_url: Some(url.into()),
            detail: detail_for_result(false, in_app),
        },
    }
}

pub async fn fetch_update_check(current: &str) -> Result<UpdateCheckResult, String> {
    let url = format!("https://api.github.com/repos/{GITHUB_REPO}/releases");
    let client = reqwest::Client::builder()
        .user_agent("TST-Desk-updater/0.1 (opt-in release check)")
        .build()
        .map_err(|e| format!("client build failed: {e}"))?;
    let releases: Vec<GhRelease> = client
        .get(&url)
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|e| format!("release fetch failed: {e}"))?
        .error_for_status()
        .map_err(|e| format!("GitHub returned an error: {e}"))?
        .json()
        .await
        .map_err(|e| format!("release JSON parse failed: {e}"))?;
    Ok(check_releases(current, &releases))
}

#[tauri::command]
pub async fn check_for_updates(app: tauri::AppHandle) -> Result<UpdateCheckResult, String> {
    let current = app
        .config()
        .version
        .clone()
        .unwrap_or_else(|| env!("CARGO_PKG_VERSION").into());
    fetch_update_check(&current).await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rel(tag: &str) -> GhRelease {
        GhRelease {
            tag_name: tag.into(),
            html_url: format!("https://github.com/{GITHUB_REPO}/releases/tag/{tag}"),
            draft: false,
            prerelease: false,
        }
    }

    #[test]
    fn ignores_non_app_tags() {
        let out = check_releases("0.1.0", &[rel("v0.2.0"), rel("tstdesk-v0.1.0")]);
        assert_eq!(out.latest.as_deref(), Some("0.1.0"));
        assert!(!out.update_available);
    }

    #[test]
    fn detects_newer_release() {
        let out = check_releases("0.1.0", &[rel("tstdesk-v0.2.0")]);
        assert!(out.update_available);
        assert_eq!(out.latest.as_deref(), Some("0.2.0"));
        assert!(!out.in_app_install_enabled);
        assert!(out.detail.contains("unsigned"));
    }

    #[test]
    fn compare_versions_orders() {
        assert_eq!(compare_versions("0.1.0", "0.2.0"), Ordering::Less);
        assert_eq!(compare_versions("1.0.0", "0.9.9"), Ordering::Greater);
    }
}
