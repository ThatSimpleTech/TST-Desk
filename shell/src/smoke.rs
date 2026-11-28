//! A release-build smoke check of the windows the host opens (WEBKIT-03).
//!
//! Every UI test ran against the dev server, so a window URL the release
//! build forms differently (WEBKIT-01: the per-session window was a 404 in
//! every shipped build) had no test that could see it. With
//! `TST_DESK_SMOKE_REPORT=<file>` set, the packaged app opens a session
//! window through the same code the rail's menu uses, asks each window
//! whether the app shell rendered, writes the answers to that file as one
//! JSON object, and quits. Without the variable none of this runs.

use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Manager};

const ENV: &str = "TST_DESK_SMOKE_REPORT";
const SESSION: &str = "smoke";
const WINDOWS: [&str; 2] = ["main", "session-smoke"];

/// Run in each window: is the app shell there, or the router's error page?
const PROBE: &str = r#"
(function () {
  const report = () => {
    const shell = document.querySelector('.shell-gear') !== null;
    const text = (document.body && document.body.innerText || '').trim();
    window.__TAURI_INTERNALS__.invoke('smoke_report', {
      url: location.href,
      ok: shell && !/^404\b/.test(text),
      text: text.slice(0, 200),
    });
  };
  if (document.readyState === 'complete') setTimeout(report, 1500);
  else window.addEventListener('load', () => setTimeout(report, 1500));
})();
"#;

#[derive(Default)]
pub struct SmokeState(Mutex<BTreeMap<String, Value>>);

pub fn enabled() -> bool {
    std::env::var_os(ENV).is_some()
}

/// Open the session window and probe every window, once the app is up.
pub fn start(app: &AppHandle) {
    if !enabled() {
        return;
    }
    app.manage(SmokeState::default());
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(4));
        if let Err(e) = crate::session_window::open_session_window(app.clone(), SESSION.into()) {
            finish(&app, json!({ "error": e }));
            return;
        }
        for _ in 0..20 {
            std::thread::sleep(Duration::from_secs(2));
            for label in WINDOWS {
                if let Some(window) = app.get_webview_window(label) {
                    let _ = window.eval(PROBE);
                }
            }
        }
        finish(&app, json!({ "error": "no answer from every window in 40 s" }));
    });
}

/// A window's answer. Ignored unless the smoke run is on.
#[tauri::command]
pub fn smoke_report(window: tauri::WebviewWindow, url: String, ok: bool, text: String) {
    if !enabled() {
        return;
    }
    let app = window.app_handle().clone();
    let Some(state) = app.try_state::<SmokeState>() else {
        return;
    };
    let done = {
        let mut seen = state.0.lock().unwrap_or_else(|e| e.into_inner());
        seen.insert(window.label().to_string(), json!({ "url": url, "ok": ok, "text": text }));
        WINDOWS.iter().all(|w| seen.contains_key(*w)).then(|| json!(seen.clone()))
    };
    if let Some(windows) = done {
        finish(&app, json!({ "windows": windows }));
    }
}

fn finish(app: &AppHandle, report: Value) {
    if let Some(path) = std::env::var_os(ENV) {
        let _ = std::fs::write(path, report.to_string());
    }
    app.exit(0);
}
