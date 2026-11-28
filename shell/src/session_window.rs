//! Secondary session windows (TD-4703).

use tauri::webview::WebviewWindowBuilder;
use tauri::{AppHandle, Manager, Url, WebviewUrl};

pub const BIND_QUERY: &str = "bind_session";

/// Stable label for a session-bound viewer window.
pub fn session_window_label(session_id: &str) -> String {
    format!("session-{session_id}")
}

pub fn is_session_window_label(label: &str) -> bool {
    label.starts_with("session-")
}

/// The root route of the app at *base* with `?bind_session=<id>` (WEBKIT-01).
///
/// Release builds gave the window the app path `index.html?bind_session=…`.
/// Tauri maps only the exact path `index.html` to the root; anything else is
/// joined as written, so the window opened `/index.html?…`, which the
/// single-page app answers with its 404 page. The root route with the query
/// is what the main window and the dev server both load.
pub fn session_url_from(base: &Url, session_id: &str) -> Url {
    let mut url = base.clone();
    url.set_path("/");
    url.set_fragment(None);
    url.query_pairs_mut().clear().append_pair(BIND_QUERY, session_id);
    url
}

/// The app URL a window loads: the custom protocol on Linux and macOS,
/// `http(s)://tauri.localhost` on Windows, the dev server under `tauri dev`.
fn app_base(app: &AppHandle) -> Url {
    if let Some(url) = app.get_webview_window("main").and_then(|w| w.url().ok()) {
        return url;
    }
    let fallback = if cfg!(debug_assertions) {
        "http://localhost:5173/"
    } else if cfg!(windows) {
        "http://tauri.localhost/"
    } else {
        "tauri://localhost/"
    };
    Url::parse(fallback).expect("valid app url")
}

fn session_window_url(app: &AppHandle, session_id: &str) -> WebviewUrl {
    let url = session_url_from(&app_base(app), session_id);
    if matches!(url.scheme(), "http" | "https") {
        WebviewUrl::External(url)
    } else {
        WebviewUrl::CustomProtocol(url)
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

    WebviewWindowBuilder::new(&app, &label, session_window_url(&app, cleaned))
        .title("TST Desk")
        .inner_size(1200.0, 800.0)
        .resizable(true)
        .build()
        .map_err(|e| format!("open session window: {e}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(text: &str) -> Url {
        Url::parse(text).unwrap()
    }

    #[test]
    fn a_session_window_loads_the_root_route_with_its_session() {
        for (base, want) in [
            ("tauri://localhost", "tauri://localhost/?bind_session=s-1"),
            ("tauri://localhost/index.html", "tauri://localhost/?bind_session=s-1"),
            ("http://tauri.localhost/", "http://tauri.localhost/?bind_session=s-1"),
            ("https://tauri.localhost/index.html?x=1#top", "https://tauri.localhost/?bind_session=s-1"),
            ("http://localhost:5173/", "http://localhost:5173/?bind_session=s-1"),
        ] {
            assert_eq!(session_url_from(&url(base), "s-1").as_str(), want, "{base}");
        }
    }

    #[test]
    fn the_path_is_never_index_html() {
        let made = session_url_from(&url("tauri://localhost/index.html"), "abc");
        assert_eq!(made.path(), "/");
        assert_eq!(
            made.query_pairs().collect::<Vec<_>>(),
            vec![("bind_session".into(), "abc".into())]
        );
    }

    #[test]
    fn a_session_id_is_encoded_not_spliced() {
        let made = session_url_from(&url("tauri://localhost/"), "a&b=c");
        assert_eq!(made.query(), Some("bind_session=a%26b%3Dc"));
    }
}
