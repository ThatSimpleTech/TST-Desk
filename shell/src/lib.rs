pub mod daemon;
mod read_text;

use daemon::embeddings::EmbeddingsHandle;
use daemon::DaemonHandle;
use read_text::read_text_file;
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

/// Open a local path with the OS default handler (TD-1202: the decisions
/// pane links out to the workspace ledger file). Zero-dependency — three
/// platform spellings of "default handler, please". Fire-and-forget: we
/// spawn and report spawn failure only; the handler's own outcome is the
/// OS's business.
#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    let spawn = || -> std::io::Result<std::process::Child> {
        #[cfg(target_os = "macos")]
        return std::process::Command::new("open").arg(&path).spawn();
        #[cfg(target_os = "windows")]
        return std::process::Command::new("cmd")
            .args(["/C", "start", "", &path])
            .spawn();
        #[cfg(all(unix, not(target_os = "macos")))]
        return std::process::Command::new("xdg-open").arg(&path).spawn();
    };
    spawn()
        .map(|_| ())
        .map_err(|e| format!("no opener available: {e}"))
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
            // Spawn and supervise the daemon for the life of the app.
            // Embeddings is a parallel supervisor (TD-2204): its death
            // never shares tstd's restart budget.
            let data_dir = daemon::data_dir();
            let handle = daemon::start(app.handle().clone(), data_dir.clone());
            let embeddings = daemon::embeddings::start(data_dir);
            app.manage(handle);
            app.manage(embeddings);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_daemon_info,
            open_path,
            read_text_file
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let app = window.app_handle().clone();
                let handle = window.app_handle().state::<DaemonHandle>().inner().clone();
                let embeddings = window
                    .app_handle()
                    .state::<EmbeddingsHandle>()
                    .inner()
                    .clone();
                // TODO(v0.3): detached-session behavior will keep the daemon
                // alive here instead of shutting it down, so sessions survive
                // the window closing.
                handle.request_shutdown();
                embeddings.request_shutdown();
                tauri::async_runtime::spawn(async move {
                    let _ = tokio::time::timeout(std::time::Duration::from_secs(10), async {
                        tokio::join!(handle.wait_for_done(), embeddings.wait_for_done());
                    })
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
                daemon::embeddings::best_effort_kill(&app_handle.state::<EmbeddingsHandle>());
            }
        });
}
