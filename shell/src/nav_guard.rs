//! The app's webviews never navigate away from the app (RENDERSEC-01).
//!
//! An SVG `<a>` or an `<area>` in a chat reply navigated the whole webview to
//! a remote page, past the chat's link guard. The sanitizer now drops both;
//! this is the wall behind it: any navigation to something that is not the
//! app's own origin is refused, whatever started it. Links meant for the
//! browser go through the opener, which this does not touch.

use tauri::plugin::{Builder, TauriPlugin};
use tauri::{Runtime, Url};

/// Whether a webview may load *url*: the app's own pages, nothing else.
pub fn allowed(url: &Url) -> bool {
    match url.scheme() {
        // Linux and macOS serve the app as tauri://localhost.
        "tauri" | "asset" | "ipc" => true,
        "about" | "data" | "blob" => true,
        // Windows serves it as http(s)://tauri.localhost; IPC as ipc.localhost.
        "http" | "https" => match url.host_str() {
            Some("tauri.localhost") | Some("ipc.localhost") => true,
            // The dev server, in a debug build only.
            Some("localhost") | Some("127.0.0.1") => cfg!(debug_assertions),
            _ => false,
        },
        _ => false,
    }
}

pub fn plugin<R: Runtime>() -> TauriPlugin<R> {
    Builder::new("nav-guard")
        .on_navigation(|_webview, url| {
            let ok = allowed(url);
            if !ok {
                log::warn!("refused a webview navigation to {}", url.scheme());
            }
            ok
        })
        .build()
}

#[cfg(test)]
mod tests {
    use super::allowed;
    use tauri::Url;

    fn ok(s: &str) -> bool {
        allowed(&Url::parse(s).unwrap())
    }

    #[test]
    fn the_apps_own_pages_load() {
        assert!(ok("tauri://localhost/"));
        assert!(ok("tauri://localhost/index.html?bind_session=abc"));
        assert!(ok("http://tauri.localhost/"));
        assert!(ok("https://tauri.localhost/200.html"));
        assert!(ok("about:blank"));
    }

    #[test]
    fn anything_else_is_refused() {
        assert!(!ok("https://example.com/"));
        assert!(!ok("http://evil.tauri.localhost.example/"));
        assert!(!ok("ftp://example.com/x"));
        assert!(!ok("file:///etc/passwd"));
        assert!(!ok("mailto:someone@example.com"));
        assert!(!ok("javascript:alert(1)"));
    }
}
