//! System tray (TD-4703): running-count tooltip and Quit.

use tauri::AppHandle;

pub const TRAY_ID: &str = "tstd-tray";

#[tauri::command]
pub fn set_tray_running_count(app: AppHandle, count: Option<u32>) {
    let Some(tray) = app.tray_by_id(TRAY_ID) else {
        return;
    };
    let tooltip = match count {
        None | Some(0) => "TST Desk".to_string(),
        Some(1) => "TST Desk — 1 session running".to_string(),
        Some(n) => format!("TST Desk — {n} sessions running"),
    };
    let _ = tray.set_tooltip(Some(tooltip));
    #[cfg(target_os = "macos")]
    {
        let title = count.filter(|n| *n > 0).map(|n| n.to_string());
        let _ = tray.set_title(title);
    }
}
