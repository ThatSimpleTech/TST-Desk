pub mod daemon;

use daemon::DaemonHandle;
use tauri::Manager;

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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            // Spawn and supervise the daemon for the life of the app.
            let handle = daemon::start(app.handle().clone(), daemon::data_dir());
            app.manage(handle);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_daemon_info])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let app = window.app_handle().clone();
                let handle = window.app_handle().state::<DaemonHandle>().inner().clone();
                // TODO(v0.3): detached-session behavior will keep the daemon
                // alive here instead of shutting it down, so sessions survive
                // the window closing.
                handle.request_shutdown();
                tauri::async_runtime::spawn(async move {
                    let _ = tokio::time::timeout(
                        std::time::Duration::from_secs(10),
                        handle.wait_for_done(),
                    )
                    .await;
                    app.exit(0);
                });
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |app_handle, event| {
            // Catch-all for quit paths that never raise CloseRequested on
            // macOS (Cmd+Q, dock quit — tauri#13778). Best-effort kill: the
            // daemon's own --parent-pid watchdog is the real no-orphan
            // guarantee; this just closes the window before the OS reaps us.
            if let tauri::RunEvent::Exit = event {
                daemon::best_effort_kill(&app_handle.state::<DaemonHandle>());
            }
        });
}