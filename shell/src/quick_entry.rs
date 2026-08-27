//! macOS quick-entry overlay (TD-4702).
//!
//! A global shortcut toggles a small always-on-top composer webview bound to
//! the last workspace the main window used. Linux/Windows are intentionally
//! out — registration and the overlay are `cfg(macos)` only.

use std::path::{Path, PathBuf};

use tauri::{AppHandle, Manager};

pub const WINDOW_LABEL: &str = "quick-entry";

#[cfg(target_os = "macos")]
use tauri::webview::WebviewWindowBuilder;
#[cfg(target_os = "macos")]
use tauri::{Emitter, WebviewUrl};

#[cfg(target_os = "macos")]
/// Emitted when registration fails — payload `{ title, body, url }`.
pub const PERMISSION_EVENT: &str = "quick-entry-permission";
#[cfg(target_os = "macos")]
pub const ACCESSIBILITY_URL: &str =
    "x-apple.systemsettings:com.apple.preferences.privacy-security.accessibility";

#[cfg(target_os = "macos")]
const PERMISSION_TITLE: &str = "Quick entry needs Accessibility";
#[cfg(target_os = "macos")]
const PERMISSION_BODY: &str = "TST Desk could not register the quick-entry \
shortcut. In System Settings → Privacy & Security → Accessibility, allow \
TST Desk, then restart the app.";

pub fn last_workspace_path(data_dir: &Path) -> PathBuf {
    data_dir.join("last-workspace.yaml")
}

/// Read `{ path: "/abs/path" }` from the user-data dir.
pub fn load_last_workspace(data_dir: &Path) -> Option<String> {
    let text = std::fs::read_to_string(last_workspace_path(data_dir)).ok()?;
    parse_last_workspace_yaml(&text)
}

fn parse_last_workspace_yaml(text: &str) -> Option<String> {
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let Some(rest) = trimmed.strip_prefix("path:") else {
            continue;
        };
        let value = rest.trim().trim_matches('"').trim_matches('\'');
        if !value.is_empty() {
            return Some(value.to_string());
        }
    }
    None
}

/// Persist the workspace path the main window last opened.
pub fn save_last_workspace(data_dir: &Path, path: &str) -> Result<(), String> {
    let cleaned = path.trim();
    if cleaned.is_empty() {
        return Err("path must not be empty".into());
    }
    std::fs::create_dir_all(data_dir).map_err(|e| format!("create data dir: {e}"))?;
    let body = format!("path: {cleaned}\n");
    std::fs::write(last_workspace_path(data_dir), body)
        .map_err(|e| format!("write last workspace: {e}"))
}

#[tauri::command]
pub fn get_last_workspace() -> Option<String> {
    load_last_workspace(&crate::daemon::data_dir())
}

#[tauri::command]
pub fn set_last_workspace(path: String) -> Result<(), String> {
    save_last_workspace(&crate::daemon::data_dir(), &path)
}

#[tauri::command]
pub fn hide_quick_entry(app: AppHandle) {
    hide_overlay(&app);
}

fn hide_overlay(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(WINDOW_LABEL) {
        let _ = window.hide();
    }
}

#[cfg(target_os = "macos")]
fn quick_entry_url() -> WebviewUrl {
    #[cfg(debug_assertions)]
    {
        WebviewUrl::External(
            "http://localhost:5173/quick-entry"
                .parse()
                .expect("valid quick-entry dev url"),
        )
    }
    #[cfg(not(debug_assertions))]
    {
        WebviewUrl::App("quick-entry/index.html".into())
    }
}

#[cfg(target_os = "macos")]
fn ensure_overlay_window(app: &AppHandle) -> tauri::Result<()> {
    if app.get_webview_window(WINDOW_LABEL).is_some() {
        return Ok(());
    }
    let window = WebviewWindowBuilder::new(app, WINDOW_LABEL, quick_entry_url())
        .title("Quick entry")
        .inner_size(560.0, 132.0)
        .resizable(false)
        .decorations(true)
        .always_on_top(true)
        .visible(false)
        .center()
        .build()?;
    let _ = window.set_skip_taskbar(true);
    Ok(())
}

#[cfg(target_os = "macos")]
fn show_overlay(app: &AppHandle) {
    if ensure_overlay_window(app).is_err() {
        return;
    }
    let Some(window) = app.get_webview_window(WINDOW_LABEL) else {
        return;
    };
    let _ = window.center();
    let _ = window.show();
    let _ = window.set_focus();
}

#[cfg(target_os = "macos")]
fn toggle_overlay(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(WINDOW_LABEL) {
        if window.is_visible().unwrap_or(false) {
            hide_overlay(app);
            return;
        }
    }
    show_overlay(app);
}

#[cfg(target_os = "macos")]
fn emit_permission_notice(app: &AppHandle) {
    let payload = serde_json::json!({
        "title": PERMISSION_TITLE,
        "body": PERMISSION_BODY,
        "url": ACCESSIBILITY_URL,
    });
    let _ = app.emit(PERMISSION_EVENT, payload);
}

#[cfg(target_os = "macos")]
pub fn setup(app: &tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    use tauri_plugin_global_shortcut::{
        Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState,
    };

    let shortcut = Shortcut::new(Some(Modifiers::SUPER | Modifiers::SHIFT), Code::Period);
    let handle = app.handle().clone();
    app.handle().plugin(
        tauri_plugin_global_shortcut::Builder::new()
            .with_handler(move |app, sc, event| {
                if sc == &shortcut && event.state() == ShortcutState::Pressed {
                    toggle_overlay(app);
                }
            })
            .build(),
    )?;

    if let Err(err) = handle.global_shortcut().register(shortcut) {
        log::warn!("quick entry shortcut registration failed: {err}");
        emit_permission_notice(&handle);
    }
    Ok(())
}

#[cfg(not(target_os = "macos"))]
pub fn setup(_app: &tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_last_workspace_yaml_reads_path() {
        assert_eq!(
            parse_last_workspace_yaml("path: /tmp/proj\n"),
            Some("/tmp/proj".into())
        );
        assert_eq!(
            parse_last_workspace_yaml("path: \"/Users/me/ws\"\n"),
            Some("/Users/me/ws".into())
        );
        assert_eq!(parse_last_workspace_yaml(""), None);
    }

    #[test]
    fn save_and_load_last_workspace_round_trip() {
        let dir = std::env::temp_dir().join(format!(
            "tstd-last-ws-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        save_last_workspace(&dir, "/work/repo").unwrap();
        assert_eq!(load_last_workspace(&dir), Some("/work/repo".into()));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn save_rejects_blank_path() {
        let dir = std::env::temp_dir().join(format!(
            "tstd-last-ws-blank-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        assert!(save_last_workspace(&dir, "   ").is_err());
        let _ = std::fs::remove_dir_all(dir);
    }
}
