//! A second launch shows the running app (TD-5024).
//!
//! Without this a relaunch was a second host with its own tray, attached
//! to the same tstd while tstd's `--parent-pid` still named the first
//! host. tauri-plugin-single-instance hands the launch to the running
//! process (D-Bus on Linux, a local socket on macOS, a named mutex and
//! window message on Windows); the second process exits in the plugin's
//! setup, before it spawns or attaches to anything.

use tauri::plugin::TauriPlugin;
use tauri::Wry;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SecondInstanceAction {
    Show,
}

/// TST Desk takes no launch arguments, so the second launch's argv and
/// cwd are ignored, and never logged.
pub(crate) fn on_second_instance() -> SecondInstanceAction {
    SecondInstanceAction::Show
}

pub(crate) fn plugin() -> TauriPlugin<Wry> {
    tauri_plugin_single_instance::init(|app, _argv, _cwd| match on_second_instance() {
        SecondInstanceAction::Show => {
            log::info!("second launch; showing the running app");
            crate::show_any_window(app);
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn second_launch_shows_the_running_app() {
        assert_eq!(on_second_instance(), SecondInstanceAction::Show);
    }
}
