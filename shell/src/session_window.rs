//! Secondary session windows (TD-4703).

use tauri::webview::WebviewWindowBuilder;
use tauri::{AppHandle, Manager, WebviewUrl};

pub const BIND_QUERY: &str = "bind_session";

/// Stable label for a session-bound viewer window.
pub fn session_window_label(session_id: &str) -> String {
    format!("session-{session_id}")
}

pub fn is_session_window_label(label: &str) -> bool {
    label.starts_with("session-")
}

fn session_window_url(session_id: &str) -> WebviewUrl {
    let query = format!("?{BIND_QUERY}={session_id}");
    #[cfg(debug_assertions)]
    {
        WebviewUrl::External(
            format!("http://localhost:5173/{query}")
                .parse()
                .expect("valid session window dev url"),
        )
    }
    #[cfg(not(debug_assertions))]
    {
        WebviewUrl::App(format!("index.html{query}").into())
    }
}

#[tauri::command]
pub fn open_session_window(app: AppHandle, session_id: String) -> Result<(), String> {
    let cleaned = session_id.trim();
    if cleaned.is_empty() {
        return Err("session_id must not be empty".into());
    }
    let label = session_window_label(cleaned);
    if let Some(window) = app.get_webview_window(&label) {
        window
            .show()
            .map_err(|e| format!("show session window: {e}"))?;
        window
            .set_focus()
            .map_err(|e| format!("focus session window: {e}"))?;
        return Ok(());
    }

    WebviewWindowBuilder::new(&app, &label, session_window_url(cleaned))
        .title("TST Desk")
        .inner_size(1200.0, 800.0)
        .resizable(true)
        .build()
        .map_err(|e| format!("open session window: {e}"))?;
    Ok(())
}
