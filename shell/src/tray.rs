//! System tray (TD-4703): running-count tooltip and Quit.

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::AppHandle;

pub const TRAY_ID: &str = "tstd-tray";

pub fn setup(app: &tauri::App) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "tray-show", "Show TST Desk", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "tray-quit", "Quit TST Desk", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &show,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;

    let Some(icon) = app.default_window_icon() else {
        log::warn!("tray setup skipped: no default window icon");
        return Ok(());
    };
    TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon.clone())
        .tooltip("TST Desk")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "tray-show" => crate::show_main_window(app),
            "tray-quit" => crate::request_quit(app),
            _ => {}
        })
        .build(app)?;

    Ok(())
}

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
