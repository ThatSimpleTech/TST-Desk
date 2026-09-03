//! OS notification with Approve / Deny for class-B approvals (TD-1702).
//!
//! The daemon still owns the session. The host only reports the click;
//! the webview sends `approve` / `deny` on `approval-action`.

use serde_json::json;
use tauri::{AppHandle, Emitter, Manager, Runtime};

const EVENT: &str = "approval-action";

#[derive(Clone, Copy)]
enum Action {
    Approve,
    Deny,
    Show,
}

pub fn spawn<R: Runtime>(
    app: AppHandle<R>,
    title: String,
    body: String,
    session_id: String,
    tool_call_id: String,
) {
    std::thread::spawn(move || {
        let action = platform_notice(&title, &body);
        emit_action(&app, &session_id, &tool_call_id, action);
        if matches!(action, Action::Show | Action::Approve | Action::Deny) {
            show_any_window(&app);
        }
    });
}

fn emit_action<R: Runtime>(
    app: &AppHandle<R>,
    session_id: &str,
    tool_call_id: &str,
    action: Action,
) {
    let name = match action {
        Action::Approve => "approve",
        Action::Deny => "deny",
        Action::Show => "show",
    };
    let _ = app.emit(
        EVENT,
        json!({
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "action": name,
        }),
    );
}

fn show_any_window<R: Runtime>(app: &AppHandle<R>) {
    let window = app
        .get_webview_window("main")
        .or_else(|| app.webview_windows().into_values().next());
    if let Some(window) = window {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg(target_os = "macos")]
fn platform_notice(title: &str, body: &str) -> Action {
    use mac_notification_sys::{MainButton, Notification, NotificationResponse};
    let identifier = if tauri::is_dev() {
        "com.apple.Terminal"
    } else {
        "com.thatsimpletech.tstdesk"
    };
    let _ = mac_notification_sys::set_application(identifier);
    match Notification::new()
        .title(title)
        .message(body)
        .main_button(MainButton::SingleAction("Approve"))
        .close_button("Deny")
        .send()
    {
        Ok(NotificationResponse::ActionButton(_)) => Action::Approve,
        Ok(NotificationResponse::CloseButton(_)) => Action::Deny,
        Ok(NotificationResponse::Click) | Ok(_) => Action::Show,
        Err(_) => Action::Show,
    }
}

#[cfg(not(target_os = "macos"))]
fn platform_notice(title: &str, body: &str) -> Action {
    let mut notification = notify_rust::Notification::new();
    notification.summary(title).body(body);
    notification.action("approve", "Approve");
    notification.action("deny", "Deny");
    match notification.show() {
        Ok(handle) => {
            let mut chosen = Action::Show;
            handle.wait_for_action(|action| match action {
                "approve" => chosen = Action::Approve,
                "deny" => chosen = Action::Deny,
                _ => chosen = Action::Show,
            });
            chosen
        }
        Err(_) => Action::Show,
    }
}
