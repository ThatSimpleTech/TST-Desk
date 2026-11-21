//! Wall-limited UTF-8 read for artifact preview (TD-3202).
//!
//! `open_artifact` returns a path, not bytes. The webview must not
//! `fetch()` arbitrary URLs. This command reads a file only when the
//! resolved path sits under the session workspace or
//! `{data_dir}/sessions/{id}/`. Everything else is refused.
//!
//! The caller names the workspace, and a caller is whatever script runs
//! in the webview, so the name is not taken on trust (TD-5293): it must be
//! a folder the daemon itself recorded as a session's workspace or
//! worktree in `{data_dir}/sessions.json`. Before this, a caller could
//! pass `/` and read any UTF-8 file the user can.

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
    allowed_roots_in(&daemon::data_dir(), workspace, session_id)
}

/// Every folder the daemon recorded as a session's workspace or worktree,
/// canonical. An absent, unreadable or malformed file gives none: the
/// workspace root is then refused rather than believed.
pub(crate) fn known_workspaces(data_dir: &Path) -> Vec<PathBuf> {
    let Ok(text) = fs::read_to_string(data_dir.join("sessions.json")) else {
        return Vec::new();
    };
    let Ok(serde_json::Value::Array(records)) = serde_json::from_str(&text) else {
        return Vec::new();
    };
    let mut known = Vec::new();
    for record in &records {
        let named = [
            record.get("workspace_path"),
            record.get("worktree").and_then(|w| w.get("path")),
        ];
        for path in named.into_iter().flatten().filter_map(|v| v.as_str()) {
            if path.is_empty() {
                continue;
            }
            if let Ok(root) = fs::canonicalize(path) {
                if !known.contains(&root) {
                    known.push(root);
                }
            }
        }
    }
    known
}

fn allowed_roots_in(
    data_dir: &Path,
    workspace: Option<&str>,
    session_id: Option<&str>,
) -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Some(ws) = workspace.filter(|s| !s.is_empty()) {
        if let Ok(root) = fs::canonicalize(ws) {
            // Exactly a recorded folder: not a parent of one, not a child.
            if known_workspaces(data_dir).contains(&root) {
                roots.push(root);
            }
        }
    }
    if let Some(sid) = session_id.and_then(safe_session_id) {
        let persist = data_dir.join("sessions").join(sid);
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

/// A session dir name, or None. ':' is refused too (TD-5015): a bridged id
/// (`<raw>:mch_…`) names a session on another computer, so it never has a
/// local session dir, and on Windows ':' would name an alternate data stream.
pub(crate) fn safe_session_id(id: &str) -> Option<&str> {
    if id.is_empty()
        || id == "."
        || id == ".."
        || id.contains('/')
        || id.contains('\\')
        || id.contains(':')
    {
        return None;
    }
    Some(id)
}

pub(crate) fn is_within(root: &Path, candidate: &Path) -> bool {
    candidate.strip_prefix(root).is_ok()
}

#[cfg(test)]
mod tests {
    use super::{allowed_roots_in, known_workspaces, safe_session_id};
    use std::fs;
    use std::path::PathBuf;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "tst-read-text-{}-{}-{}",
            name,
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        fs::create_dir_all(&dir).unwrap();
        fs::canonicalize(dir).unwrap()
    }

    fn record(data: &std::path::Path, body: &str) {
        fs::write(data.join("sessions.json"), body).unwrap();
    }

    #[test]
    fn a_workspace_the_daemon_never_recorded_is_no_root() {
        let base = scratch("unknown");
        let data = base.join("data");
        let opened = base.join("opened");
        let other = base.join("other");
        for dir in [&data, &opened, &other] {
            fs::create_dir_all(dir).unwrap();
        }
        let body = serde_json::json!([
            {"session_id": "s1", "workspace_path": opened.to_str().unwrap(), "worktree": null}
        ]);
        record(&data, &body.to_string());

        let ok = allowed_roots_in(&data, opened.to_str(), None);
        assert_eq!(ok, vec![opened.clone()]);
        // What the hole was: any folder the caller cared to name.
        for named in [other.to_str().unwrap(), base.to_str().unwrap(), "/"] {
            assert!(allowed_roots_in(&data, Some(named), None).is_empty(), "{named}");
        }
        // A folder inside a recorded workspace is not itself a root either.
        let inner = opened.join("src");
        fs::create_dir_all(&inner).unwrap();
        assert!(allowed_roots_in(&data, inner.to_str(), None).is_empty());
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn a_sessions_worktree_folder_is_a_root_too() {
        let base = scratch("worktree");
        let data = base.join("data");
        let project = base.join("project");
        let tree = base.join("tree");
        for dir in [&data, &project, &tree] {
            fs::create_dir_all(dir).unwrap();
        }
        let body = serde_json::json!([{
            "session_id": "s1",
            "workspace_path": project.to_str().unwrap(),
            "worktree": {"path": tree.to_str().unwrap(), "branch": "tst/wt/x"}
        }]);
        record(&data, &body.to_string());
        let known = known_workspaces(&data);
        assert!(known.contains(&project) && known.contains(&tree));
        assert_eq!(allowed_roots_in(&data, tree.to_str(), None), vec![tree.clone()]);
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn no_record_or_a_broken_one_allows_no_workspace() {
        let base = scratch("broken");
        let data = base.join("data");
        let opened = base.join("opened");
        fs::create_dir_all(&data).unwrap();
        fs::create_dir_all(&opened).unwrap();
        assert!(allowed_roots_in(&data, opened.to_str(), None).is_empty());
        for body in ["", "{not json", "{\"sessions\": []}", "[1, \"x\", {\"workspace_path\": 3}]"] {
            record(&data, body);
            assert!(known_workspaces(&data).is_empty(), "{body}");
            assert!(allowed_roots_in(&data, opened.to_str(), None).is_empty(), "{body}");
        }
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn the_session_dir_root_does_not_depend_on_the_record() {
        let base = scratch("session");
        let data = base.join("data");
        let persist = data.join("sessions").join("abc");
        fs::create_dir_all(&persist).unwrap();
        let roots = allowed_roots_in(&data, None, Some("abc"));
        assert_eq!(roots, vec![fs::canonicalize(&persist).unwrap()]);
        assert!(allowed_roots_in(&data, None, Some("../abc")).is_empty());
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn safe_session_id_keeps_a_plain_uuid() {
        let id = "0f8c2a4e-1b3d-4c5e-8f90-123456789abc";
        assert_eq!(safe_session_id(id), Some(id));
    }

    #[test]
    fn safe_session_id_refuses_traversal_and_separators() {
        for id in ["", ".", "..", "a/b", "a\\b", "../x"] {
            assert_eq!(safe_session_id(id), None, "{id:?}");
        }
    }

    #[test]
    fn safe_session_id_refuses_a_bridged_id() {
        assert_eq!(safe_session_id("0f8c2a4e-1b3d:mch_1a2b3c4d5e6f"), None);
        assert_eq!(safe_session_id("a:b"), None);
        assert_eq!(safe_session_id(":"), None);
    }
}
