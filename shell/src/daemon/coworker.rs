//! Coworker mode (TD-2902): close hides the window; quit still shuts down.
//! `--parent-pid` is always passed (TD-2903); close keeps the host alive.
//!
//! The bit lives at `{user_data_dir}/coworker.yaml` as `{enabled: true}`,
//! the same shape as skip-all. Default on when the file is missing.
//! Settings UI is TD-2905 — this module only loads the file.

use std::path::Path;

/// What the host should do with a window-lifecycle event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CloseAction {
    Hide,
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
    match event {
        LifecycleEvent::Quit => CloseAction::Shutdown,
        LifecycleEvent::CloseRequested if coworker_on => CloseAction::Hide,
        LifecycleEvent::CloseRequested => CloseAction::Shutdown,
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
    let Ok(root) = serde_yaml::from_str::<serde_yaml::Value>(text) else {
        return true;
    };
    let Some(map) = root.as_mapping() else {
        return true;
    };
    match map.get(serde_yaml::Value::from("enabled")) {
        None => true,
        Some(serde_yaml::Value::Bool(b)) => *b,
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
