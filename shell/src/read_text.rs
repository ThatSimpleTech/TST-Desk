//! Wall-limited UTF-8 read for artifact preview (TD-3202).
//!
//! `open_artifact` returns a path, not bytes. The webview must not
//! `fetch()` arbitrary URLs. This command reads a file only when the
//! resolved path sits under the session workspace or
//! `{data_dir}/sessions/{id}/`. Everything else is refused.

use std::fs;
use std::path::{Path, PathBuf};

use crate::daemon;

const MAX_PREVIEW_BYTES: u64 = 2 * 1024 * 1024;

#[tauri::command]
pub fn read_text_file(
    path: String,
    workspace: Option<String>,
    session_id: Option<String>,
) -> Result<String, String> {
    let roots = allowed_roots(workspace.as_deref(), session_id.as_deref());
    if roots.is_empty() {
        return Err("refused: no workspace or session dir to read under".into());
    }
    let requested = PathBuf::from(&path);
    let candidates = candidates(&requested, workspace.as_deref(), session_id.as_deref());
    for candidate in candidates {
        let Ok(resolved) = fs::canonicalize(&candidate) else {
            continue;
        };
        if !roots.iter().any(|root| is_within(root, &resolved)) {
            continue;
        }
        if !resolved.is_file() {
            return Err("refused: not a file".into());
        }
        let meta = fs::metadata(&resolved).map_err(|e| e.to_string())?;
        if meta.len() > MAX_PREVIEW_BYTES {
            return Err("too large to preview".into());
        }
        return fs::read_to_string(&resolved).map_err(|e| e.to_string());
    }
    Err("refused: path is outside the workspace or session artifact dir".into())
}

fn allowed_roots(workspace: Option<&str>, session_id: Option<&str>) -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Some(ws) = workspace.filter(|s| !s.is_empty()) {
        if let Ok(root) = fs::canonicalize(ws) {
            roots.push(root);
        }
    }
    if let Some(sid) = session_id.and_then(safe_session_id) {
        let persist = daemon::data_dir().join("sessions").join(sid);
        if let Ok(root) = fs::canonicalize(persist) {
            roots.push(root);
        }
    }
    roots
}

fn candidates(requested: &Path, workspace: Option<&str>, session_id: Option<&str>) -> Vec<PathBuf> {
    if requested.is_absolute() {
        return vec![requested.to_path_buf()];
    }
    let mut out = Vec::new();
    if let Some(ws) = workspace.filter(|s| !s.is_empty()) {
        out.push(PathBuf::from(ws).join(requested));
    }
    if let Some(sid) = session_id.and_then(safe_session_id) {
        out.push(
            daemon::data_dir()
                .join("sessions")
                .join(sid)
                .join(requested),
        );
    }
    out
}

fn safe_session_id(id: &str) -> Option<&str> {
    if id.is_empty() || id == "." || id == ".." || id.contains('/') || id.contains('\\') {
        return None;
    }
    Some(id)
}

fn is_within(root: &Path, candidate: &Path) -> bool {
    candidate.strip_prefix(root).is_ok()
}
