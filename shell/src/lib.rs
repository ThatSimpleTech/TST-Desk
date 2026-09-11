mod approval_notice;
pub(crate) mod cu_agent;
#[cfg(target_os = "macos")]
mod cu_ax;
pub mod cu_identity;
pub mod daemon;
mod read_text;

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use daemon::close_hint::{
    take_first_close_hint, CLOSE_HINT_BODY, CLOSE_HINT_EVENT, CLOSE_HINT_TITLE,
};
#[cfg(not(target_os = "macos"))]
use daemon::coworker::{indicator_badge_count, indicator_window_title};
use daemon::coworker::{window_close_action_with_count, CloseAction, LifecycleEvent};
use daemon::embeddings::EmbeddingsHandle;
use daemon::DaemonHandle;
use read_text::read_text_file;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

/// Set at the start of [`request_quit`] so CloseRequested during Quit
/// does not hide, and a second ExitRequested is allowed to finish.
static QUITTING: AtomicBool = AtomicBool::new(false);
static WINDOW_SEQ: AtomicU32 = AtomicU32::new(2);

fn begin_quit() -> bool {
    !QUITTING.swap(true, Ordering::SeqCst)
}

fn is_quitting() -> bool {
    QUITTING.load(Ordering::SeqCst)
}

/// Expose the daemon's live connection info to the frontend (TD-1003 will
/// use the token for an authenticated session link; the port lets the UI
/// build a WS url).
#[tauri::command]
fn get_daemon_info(state: tauri::State<DaemonHandle>) -> Option<serde_json::Value> {
    state.conn_info().map(|c| {
        serde_json::json!({
            "port": c.port,
            "token": c.token,
        })
    })
}

/// Open a local path with the OS default handler (TD-1202: the decisions
/// pane links out to the workspace ledger file). Goes through the opener
/// plugin already registered on the app (TD-4809): its per-platform
/// implementation never routes the path through a shell, so a path is
/// always one argv element — nothing to inject, no quoting to get right.
#[tauri::command]
fn open_path(app: tauri::AppHandle, path: String) -> Result<(), String> {
    app.opener()
        .open_path(path, None::<&str>)
        .map_err(|e| format!("open failed: {e}"))
}

/// Palette / menu **Quit TST Desk** (TD-2903). Same path as Cmd+Q.
#[tauri::command]
fn quit_app(app: tauri::AppHandle) {
    request_quit(&app);
}

/// Dock / taskbar badge while a hidden session is still working (TD-2904).
/// Tray tooltip is `set_tray_tooltip` (TD-4703). The UI owns the copy.
const WINDOW_VISIBILITY_EVENT: &str = "window-visibility";
const TRAY_ID: &str = "main";

#[tauri::command]
fn set_coworker_indicator(app: tauri::AppHandle, label: Option<String>) {
    let window = app
        .get_webview_window("main")
        .or_else(|| app.webview_windows().into_values().next());
    let Some(window) = window else {
        return;
    };
    #[cfg(target_os = "macos")]
    {
        let _ = window.set_badge_label(label);
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = window.set_badge_count(indicator_badge_count(label.as_deref()));
        let _ = window.set_title(&indicator_window_title(label.as_deref()));
    }
}

fn emit_window_visibility(app: &tauri::AppHandle, visible: bool) {
    let _ = app.emit(WINDOW_VISIBILITY_EVENT, visible);
}

fn show_any_window(app: &tauri::AppHandle) {
    let window = app
        .get_webview_window("main")
        .or_else(|| app.webview_windows().into_values().next());
    if let Some(window) = window {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        // The window is up — the badge must not keep claiming work.
        // The UI also clears; this covers a napping webview.
        #[cfg(target_os = "macos")]
        {
            let _ = window.set_badge_label(None);
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = window.set_badge_count(None);
            let _ = window.set_title(&indicator_window_title(None));
        }
        emit_window_visibility(app, true);
    }
}

#[tauri::command]
fn set_tray_tooltip(app: tauri::AppHandle, tooltip: String) {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let _ = tray.set_tooltip(Some(&tooltip));
    }
}

#[tauri::command]
fn new_desk_window(app: tauri::AppHandle) -> Result<(), String> {
    open_desk_window(&app).map_err(|e| e.to_string())
}

#[tauri::command]
fn approval_notice(
    app: tauri::AppHandle,
    title: String,
    body: String,
    session_id: String,
    tool_call_id: String,
) {
    approval_notice::spawn(app, title, body, session_id, tool_call_id);
}

fn open_desk_window(app: &tauri::AppHandle) -> tauri::Result<()> {
    let n = WINDOW_SEQ.fetch_add(1, Ordering::SeqCst);
    let label = format!("desk-{n}");
    WebviewWindowBuilder::new(app, &label, WebviewUrl::App("index.html".into()))
        .title("TST Desk")
        .inner_size(1200.0, 800.0)
        .build()?;
    Ok(())
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let h = app.handle();
    let show = MenuItem::with_id(h, "tray-show", "Show TST Desk", true, None::<&str>)?;
    let new_win = MenuItem::with_id(h, "tray-new-window", "New window", true, None::<&str>)?;
    let quit = MenuItem::with_id(h, "tray-quit", "Quit TST Desk", true, None::<&str>)?;
    let menu = Menu::with_items(h, &[&show, &new_win, &quit])?;
    let Some(icon) = app.default_window_icon().cloned() else {
        log::warn!("no default window icon; tray not created");
        return Ok(());
    };
    TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon)
        .menu(&menu)
        .tooltip("TST Desk")
        .on_menu_event(|app, event| match event.id().as_ref() {
            "tray-show" => show_any_window(app),
            "tray-new-window" => {
                let _ = open_desk_window(app);
            }
            "tray-quit" => request_quit(app),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_any_window(tray.app_handle());
            }
        })
        .build(app)?;
    Ok(())
}

fn request_quit(app: &tauri::AppHandle) {
    if !begin_quit() {
        return;
    }
    let handle = app.state::<DaemonHandle>().inner().clone();
    let embeddings = app.state::<EmbeddingsHandle>().inner().clone();
    handle.request_shutdown();
    embeddings.request_shutdown();
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let _ = tokio::time::timeout(std::time::Duration::from_secs(10), async {
            tokio::join!(handle.wait_for_done(), embeddings.wait_for_done());
        })
        .await;
        app.exit(0);
    });
}

/// First hide only: stamp the user-data flag and say close is not quit.
fn maybe_notice_close_is_not_quit(app: &tauri::AppHandle) {
    if !take_first_close_hint(&daemon::data_dir()) {
        return;
    }
    let payload = serde_json::json!({
        "title": CLOSE_HINT_TITLE,
        "body": CLOSE_HINT_BODY,
    });
    let _ = app.emit(CLOSE_HINT_EVENT, payload);
    let _ = app
        .notification()
        .builder()
        .title(CLOSE_HINT_TITLE)
        .body(CLOSE_HINT_BODY)
        .show();
}

fn build_app_menu(app: &tauri::App) -> tauri::Result<Menu<tauri::Wry>> {
    let h = app.handle();
    let quit = MenuItem::with_id(
        h,
        "quit-tst-desk",
        "Quit TST Desk",
        true,
        Some("CmdOrCtrl+Q"),
    )?;
    let edit = Submenu::with_items(
        h,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(h, None)?,
            &PredefinedMenuItem::redo(h, None)?,
            &PredefinedMenuItem::separator(h)?,
            &PredefinedMenuItem::cut(h, None)?,
            &PredefinedMenuItem::copy(h, None)?,
            &PredefinedMenuItem::paste(h, None)?,
            &PredefinedMenuItem::select_all(h, None)?,
        ],
    )?;

    #[cfg(target_os = "macos")]
    {
        let app_menu = Submenu::with_items(
            h,
            "TST Desk",
            true,
            &[
                &PredefinedMenuItem::about(h, None, None)?,
                &PredefinedMenuItem::separator(h)?,
                &PredefinedMenuItem::hide(h, None)?,
                &PredefinedMenuItem::hide_others(h, None)?,
                &PredefinedMenuItem::separator(h)?,
                &quit,
            ],
        )?;
        let window = Submenu::with_items(
            h,
            "Window",
            true,
            &[
                &PredefinedMenuItem::minimize(h, None)?,
                &PredefinedMenuItem::close_window(h, None)?,
            ],
        )?;
        Menu::with_items(h, &[&app_menu, &edit, &window])
    }

    #[cfg(not(target_os = "macos"))]
    {
        let file = Submenu::with_items(h, "File", true, &[&quit])?;
        Menu::with_items(h, &[&file, &edit])
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_dialog::init())
        // TD-1201: the stack panel opens resolved steering files in the
        // system editor. Paths come from the daemon's assembled stack.
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            app.set_menu(build_app_menu(app)?)?;
            if let Err(e) = build_tray(app) {
                log::warn!("tray icon not created: {e}");
            }
            // Spawn and supervise the daemon for the life of the app.
            // Embeddings is a parallel supervisor (TD-2204): its death
            // never shares tstd's restart budget.
            let data_dir = daemon::data_dir();
            // Bind the CU socket first so tstd does not take over actuation.
            cu_agent::bind_and_serve(&data_dir);
            let handle = daemon::start(app.handle().clone(), data_dir.clone());
            let embeddings = daemon::embeddings::start(data_dir);
            app.manage(handle);
            app.manage(embeddings);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_daemon_info,
            open_path,
            read_text_file,
            quit_app,
            set_coworker_indicator,
            set_tray_tooltip,
            new_desk_window,
            approval_notice
        ])
        .on_menu_event(|app, event| {
            if event.id() == "quit-tst-desk" {
                request_quit(app);
            }
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if is_quitting() {
                    return;
                }
                let coworker_on = daemon::load_coworker(&daemon::data_dir());
                let count = window.app_handle().webview_windows().len();
                match window_close_action_with_count(
                    LifecycleEvent::CloseRequested,
                    coworker_on,
                    count,
                ) {
                    CloseAction::Destroy => {}
                    CloseAction::Hide => {
                        api.prevent_close();
                        // Close ≠ quit. The host stays up so reopen does
                        // not spawn a second process; tstd keeps running.
                        let _ = window.hide();
                        emit_window_visibility(window.app_handle(), false);
                        maybe_notice_close_is_not_quit(window.app_handle());
                    }
                    CloseAction::Shutdown => {
                        api.prevent_close();
                        request_quit(window.app_handle());
                    }
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |app_handle, event| {
            match event {
                // Cmd+Q / dock Quit / menu Quit TST Desk — shutdown + reap.
                // Close is hide, not this.
                tauri::RunEvent::ExitRequested { api, .. } => {
                    if !is_quitting() {
                        api.prevent_exit();
                        request_quit(app_handle);
                    }
                }
                tauri::RunEvent::Exit => {
                    daemon::best_effort_kill(&app_handle.state::<DaemonHandle>());
                    daemon::embeddings::best_effort_kill(&app_handle.state::<EmbeddingsHandle>());
                }
                #[cfg(target_os = "macos")]
                tauri::RunEvent::Reopen { .. } => show_any_window(app_handle),
                _ => {}
            }
        });
}
