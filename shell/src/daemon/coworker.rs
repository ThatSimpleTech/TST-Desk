//! Coworker mode (TD-2902): close hides the window; quit still shuts down.
//! `--parent-pid` is always passed (TD-2903); close keeps the host alive.
//! Closing windows in any order stays in the tray (TD-5020).
//!
//! The bit lives at `{user_data_dir}/coworker.yaml` as `{enabled: true}`,
//! the same shape as skip-all. Default on when the file is missing.
//! Settings UI is TD-2905 — this module only loads the file.

use std::path::Path;

/// What the host should do with a window-lifecycle event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CloseAction {
    Hide,
    /// Let this window die. The daemon and other windows stay up.
    Destroy,
    Shutdown,
}

/// Host lifecycle events that decide daemon fate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LifecycleEvent {
    CloseRequested,
    Quit,
}

/// Window title while the coworker indicator is up (taskbar tooltip).
pub fn indicator_window_title(label: Option<&str>) -> String {
    match label {
        Some(text) => format!("TST Desk — {text}"),
        None => "TST Desk".into(),
    }
}

/// Count badge for platforms that cannot show a text dock label.
pub fn indicator_badge_count(label: Option<&str>) -> Option<i64> {
    label.map(|_| 1)
}

/// Close hides when coworker is on. Quit always shuts down.
pub fn window_close_action(event: LifecycleEvent, coworker_on: bool) -> CloseAction {
    window_close_action_with_count(event, coworker_on, 1)
}

/// Extra windows destroy; only the last window hides or quits (TD-4703).
pub fn window_close_action_with_count(
    event: LifecycleEvent,
    coworker_on: bool,
    window_count: usize,
) -> CloseAction {
    match event {
        LifecycleEvent::Quit => CloseAction::Shutdown,
        LifecycleEvent::CloseRequested if window_count > 1 => CloseAction::Destroy,
        LifecycleEvent::CloseRequested if coworker_on => CloseAction::Hide,
        LifecycleEvent::CloseRequested => CloseAction::Shutdown,
    }
}

/// Label of the window `tauri.conf.json` opens at launch.
pub const MAIN_WINDOW_LABEL: &str = "main";

/// Windows that count toward "last window" (TD-5020). The hidden
/// quick-entry overlay is not a viewer: counting it made closing the
/// main window destroy it instead of hiding it.
pub fn viewer_window_count<'a>(labels: impl IntoIterator<Item = &'a str>) -> usize {
    labels
        .into_iter()
        .filter(|label| *label != crate::quick_entry::WINDOW_LABEL)
        .count()
}

/// What the host does with `RunEvent::ExitRequested` (TD-5020).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExitAction {
    /// Keep the host and tstd up with no window; the tray brings one back.
    StayInTray,
    /// Let the process exit. Quit has already stopped tstd.
    Exit,
    /// Prevent this exit and run Quit, which stops tstd first.
    Shutdown,
}

/// Tauri raises ExitRequested with `code: None` when the last window is
/// destroyed, and with `Some` from `app.exit`. Close-to-hide does not
/// cover every order: a session window closes without the hide rule,
/// and an extra main window is destroyed. So a user-caused exit stays
/// in the tray while Keep running is on. With coworker off, or with no
/// tray to come back through, it quits as before.
pub fn exit_requested_action(
    code: Option<i32>,
    quitting: bool,
    coworker_on: bool,
    tray_ok: bool,
) -> ExitAction {
    if quitting {
        return ExitAction::Exit;
    }
    match code {
        None if coworker_on && tray_ok => ExitAction::StayInTray,
        _ => ExitAction::Shutdown,
    }
}

/// Which window Show (tray, Reopen, a second launch) raises.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ShowTarget {
    Existing(String),
    /// Every viewer was closed while Keep running stayed on.
    OpenNew,
    /// Quit is under way; a new window would only be torn down.
    Nothing,
}

/// Main first, then another desk window, then a session window. The
/// quick-entry overlay is never the window Show brings back.
pub fn show_target<'a>(labels: impl IntoIterator<Item = &'a str>, quitting: bool) -> ShowTarget {
    let rank = |label: &str| {
        if label == MAIN_WINDOW_LABEL {
            0
        } else if crate::session_window::is_session_window_label(label) {
            2
        } else {
            1
        }
    };
    let best = labels
        .into_iter()
        .filter(|label| *label != crate::quick_entry::WINDOW_LABEL)
        .min_by(|a, b| rank(a).cmp(&rank(b)).then_with(|| a.cmp(b)));
    match best {
        Some(label) => ShowTarget::Existing(label.to_string()),
        None if quitting => ShowTarget::Nothing,
        None => ShowTarget::OpenNew,
    }
}

/// `--parent-pid` argv fragment. Always armed (TD-2903).
///
/// Close hides the window and leaves the host process alive, so the
/// watchdog stays quiet. Force-quit / SIGKILL of the host is what
/// trips it — omitting the flag would leave an orphan listener.
pub fn parent_pid_argv(host_pid: u32) -> Vec<String> {
    vec!["--parent-pid".into(), host_pid.to_string()]
}

/// True when `port.json` names a live listener — attach; do not spawn.
pub fn should_spawn_new_daemon(port_pid: u32, pid_alive: bool) -> bool {
    !(port_pid > 0 && pid_alive)
}

/// Load coworker mode from `{data_dir}/coworker.yaml`. Absent or
/// unreadable is on. Only YAML `true`/`false` count as an explicit bit.
pub fn load_coworker(data_dir: &Path) -> bool {
    let path = data_dir.join("coworker.yaml");
    let Ok(text) = std::fs::read_to_string(&path) else {
        return true;
    };
    coworker_enabled_from_yaml(&text)
}

/// Write `{enabled: true}` if the file is missing. Leaves an existing
/// file untouched so a later Settings off-switch (TD-2905) sticks.
pub fn ensure_coworker_file(data_dir: &Path) {
    let path = data_dir.join("coworker.yaml");
    if path.exists() {
        return;
    }
    if let Err(e) = std::fs::create_dir_all(data_dir) {
        log::warn!("could not create data dir for coworker.yaml: {e}");
        return;
    }
    if let Err(e) = std::fs::write(&path, "enabled: true\n") {
        log::warn!("could not persist default coworker.yaml: {e}");
    }
}

fn coworker_enabled_from_yaml(text: &str) -> bool {
    let Ok(root) = serde_yml::from_str::<serde_yml::Value>(text) else {
        return true;
    };
    let Some(map) = root.as_mapping() else {
        return true;
    };
    match map.get("enabled") {
        None => true,
        Some(serde_yml::Value::Bool(b)) => *b,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn indicator_title_and_count_follow_the_label() {
        assert_eq!(indicator_window_title(None), "TST Desk");
        assert_eq!(
            indicator_window_title(Some("Running")),
            "TST Desk — Running"
        );
        assert_eq!(
            indicator_window_title(Some("Approval needed")),
            "TST Desk — Approval needed"
        );
        assert_eq!(indicator_badge_count(None), None);
        assert_eq!(indicator_badge_count(Some("Running")), Some(1));
    }

    #[test]
    fn close_hides_when_coworker_on() {
        assert_eq!(
            window_close_action(LifecycleEvent::CloseRequested, true),
            CloseAction::Hide
        );
    }

    #[test]
    fn extra_window_destroys_without_quitting() {
        assert_eq!(
            window_close_action_with_count(LifecycleEvent::CloseRequested, true, 2),
            CloseAction::Destroy
        );
        assert_eq!(
            window_close_action_with_count(LifecycleEvent::CloseRequested, true, 1),
            CloseAction::Hide
        );
    }

    #[test]
    fn close_shuts_down_when_coworker_off() {
        assert_eq!(
            window_close_action(LifecycleEvent::CloseRequested, false),
            CloseAction::Shutdown
        );
    }

    #[test]
    fn quit_always_shuts_down() {
        assert_eq!(
            window_close_action(LifecycleEvent::Quit, true),
            CloseAction::Shutdown
        );
        assert_eq!(
            window_close_action(LifecycleEvent::Quit, false),
            CloseAction::Shutdown
        );
    }

    #[test]
    fn exit_requested_table() {
        use ExitAction::{Exit, Shutdown, StayInTray};
        // (code, quitting, coworker_on, tray_ok) → action
        let table = [
            // Last window destroyed, Keep running on, tray up: stay.
            ((None, false, true, true), StayInTray),
            // No way back without a tray: quit as before.
            ((None, false, true, false), Shutdown),
            // Keep running off: quit as before.
            ((None, false, false, true), Shutdown),
            ((None, false, false, false), Shutdown),
            // Our own app.exit after Quit stopped tstd.
            ((Some(0), true, true, true), Exit),
            ((Some(0), true, false, false), Exit),
            // A window closing during Quit must not cancel the exit.
            ((None, true, true, true), Exit),
            // An exit nobody routed through Quit still stops tstd first.
            ((Some(0), false, true, true), Shutdown),
            ((Some(1), false, false, true), Shutdown),
        ];
        for ((code, quitting, coworker_on, tray_ok), want) in table {
            assert_eq!(
                exit_requested_action(code, quitting, coworker_on, tray_ok),
                want,
                "code={code:?} quitting={quitting} coworker={coworker_on} tray={tray_ok}"
            );
        }
    }

    #[test]
    fn quick_entry_does_not_count_as_a_viewer() {
        assert_eq!(viewer_window_count(["main", "quick-entry"]), 1);
        assert_eq!(viewer_window_count(["quick-entry"]), 0);
        assert_eq!(viewer_window_count(["main", "desk-2", "session-abc"]), 3);
        // Main plus the hidden overlay is the last window: it hides.
        assert_eq!(
            window_close_action_with_count(
                LifecycleEvent::CloseRequested,
                true,
                viewer_window_count(["main", "quick-entry"]),
            ),
            CloseAction::Hide
        );
    }

    #[test]
    fn show_prefers_main_then_desk_then_session() {
        let pick = |labels: &[&str]| show_target(labels.iter().copied(), false);
        assert_eq!(
            pick(&["session-a", "desk-3", "main", "quick-entry"]),
            ShowTarget::Existing("main".into())
        );
        assert_eq!(
            pick(&["session-a", "desk-3", "desk-2"]),
            ShowTarget::Existing("desk-2".into())
        );
        assert_eq!(
            pick(&["session-b", "session-a"]),
            ShowTarget::Existing("session-a".into())
        );
    }

    #[test]
    fn show_opens_a_desk_window_when_none_is_left() {
        assert_eq!(show_target([], false), ShowTarget::OpenNew);
        assert_eq!(show_target(["quick-entry"], false), ShowTarget::OpenNew);
        assert_eq!(show_target(["quick-entry"], true), ShowTarget::Nothing);
        assert_eq!(
            show_target(["desk-2"], true),
            ShowTarget::Existing("desk-2".into())
        );
    }

    #[test]
    fn parent_pid_always_passed() {
        assert_eq!(
            parent_pid_argv(99),
            vec!["--parent-pid".to_string(), "99".to_string()]
        );
        assert_eq!(
            parent_pid_argv(42),
            vec!["--parent-pid".to_string(), "42".to_string()]
        );
    }

    #[test]
    fn live_port_pid_does_not_spawn() {
        assert!(!should_spawn_new_daemon(7, true));
        assert!(should_spawn_new_daemon(7, false));
        assert!(should_spawn_new_daemon(0, true));
    }

    #[test]
    fn absent_or_junk_yaml_is_on() {
        assert!(coworker_enabled_from_yaml(""));
        assert!(coworker_enabled_from_yaml("::::"));
        assert!(coworker_enabled_from_yaml("- list\n"));
        assert!(coworker_enabled_from_yaml("other: true\n"));
        assert!(coworker_enabled_from_yaml("enabled: true\n"));
        assert!(!coworker_enabled_from_yaml("enabled: false\n"));
        assert!(!coworker_enabled_from_yaml("enabled: \"true\"\n"));
    }

    #[test]
    fn load_and_ensure_round_trip() {
        let dir = std::env::temp_dir().join(format!(
            "tstd-coworker-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        assert!(load_coworker(&dir));
        ensure_coworker_file(&dir);
        assert!(load_coworker(&dir));
        std::fs::write(dir.join("coworker.yaml"), "enabled: false\n").unwrap();
        assert!(!load_coworker(&dir));
        ensure_coworker_file(&dir);
        assert!(!load_coworker(&dir));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
