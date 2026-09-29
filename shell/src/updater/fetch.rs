//! GitHub fetch for the updater.
//!
//! Two requests, and nothing else: the releases list, then the asset
//! named by that list. No client id, no query beyond `per_page`.

use std::path::Path;

use futures_util::StreamExt;
use tokio::io::AsyncWriteExt;

use super::plan::{
    initial_download_allowed, parse_releases, redirect_host_allowed, releases_api_allowed,
    releases_url, sha256_hex, Release, MAX_DOWNLOAD_BYTES, MAX_SUMS_BYTES,
};

pub fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .user_agent("TST-Desk-updater/0.1 (release check)")
        .connect_timeout(std::time::Duration::from_secs(15))
        .timeout(std::time::Duration::from_secs(15 * 60))
        .redirect(reqwest::redirect::Policy::custom(|attempt| {
            if attempt.previous().len() >= 5 {
                return attempt.error("too many redirects");
            }
            let host = attempt.url().host_str().unwrap_or("").to_string();
            if redirect_host_allowed(&host) {
                attempt.follow()
            } else {
                attempt.error(format!("redirect host refused: {host}"))
            }
        }))
        .build()
        .map_err(|e| format!("client build failed: {e}"))
}

pub async fn fetch_releases(http: &reqwest::Client) -> Result<Vec<Release>, String> {
    let url = releases_url();
    if !releases_api_allowed(&url) {
        return Err("the releases URL is not the GitHub API for this repo".into());
    }
    let response = http
        .get(&url)
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|e| format!("release fetch failed: {e}"))?;
    if response.status() == reqwest::StatusCode::FORBIDDEN
        || response.status() == reqwest::StatusCode::TOO_MANY_REQUESTS
    {
        return Err("GitHub rate-limited the update check. Try again later.".into());
    }
    let response = response
        .error_for_status()
        .map_err(|e| format!("GitHub returned an error: {e}"))?;
    let body = response
        .text()
        .await
        .map_err(|e| format!("release read failed: {e}"))?;
    if body.len() > MAX_SUMS_BYTES * 5 {
        return Err("the release list was larger than expected".into());
    }
    parse_releases(&body)
}

pub async fn download_text(http: &reqwest::Client, url: &str) -> Result<String, String> {
    if !initial_download_allowed(url) {
        return Err("refused a download URL that is not this repo's release".into());
    }
    let response = send_download(http, url).await?;
    let body = response
        .text()
        .await
        .map_err(|e| format!("checksum read failed: {e}"))?;
    if body.len() > MAX_SUMS_BYTES {
        return Err("SHA256SUMS.txt was larger than expected".into());
    }
    Ok(body)
}

pub async fn download_verified(
    http: &reqwest::Client,
    url: &str,
    dest: &Path,
    expected_sha256: &str,
    mut on_progress: impl FnMut(u64, Option<u64>),
) -> Result<(), String> {
    if !initial_download_allowed(url) {
        return Err("refused a download URL that is not this repo's release".into());
    }
    let response = send_download(http, url).await?;
    let total = response.content_length();
    if total.is_some_and(|n| n > MAX_DOWNLOAD_BYTES) {
        return Err("the release asset is larger than this updater will download".into());
    }
    if let Some(parent) = dest.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| format!("could not create the update directory: {e}"))?;
    }
    let partial = dest.with_file_name(format!(
        "{}.partial",
        dest.file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("download")
    ));
    let mut file = tokio::fs::File::create(&partial)
        .await
        .map_err(|e| format!("could not write the download: {e}"))?;
    let mut got: u64 = 0;
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("download failed: {e}"))?;
        got += chunk.len() as u64;
        if got > MAX_DOWNLOAD_BYTES {
            let _ = tokio::fs::remove_file(&partial).await;
            return Err("the release asset is larger than this updater will download".into());
        }
        file.write_all(&chunk)
            .await
            .map_err(|e| format!("could not write the download: {e}"))?;
        on_progress(got, total);
    }
    file.flush()
        .await
        .map_err(|e| format!("could not finish the download: {e}"))?;
    drop(file);
    let bytes = tokio::fs::read(&partial)
        .await
        .map_err(|e| format!("could not read the download back: {e}"))?;
    if sha256_hex(&bytes) != expected_sha256.to_ascii_lowercase() {
        let _ = tokio::fs::remove_file(&partial).await;
        return Err(super::plan::checksum_failed_detail().into());
    }
    tokio::fs::rename(&partial, dest)
        .await
        .map_err(|e| format!("could not save the download: {e}"))?;
    Ok(())
}

async fn send_download(http: &reqwest::Client, url: &str) -> Result<reqwest::Response, String> {
    let response = http
        .get(url)
        .send()
        .await
        .map_err(|e| format!("download failed: {e}"))?;
    response
        .error_for_status()
        .map_err(|e| format!("download failed: {e}"))
}
