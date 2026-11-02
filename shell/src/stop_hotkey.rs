//! System-wide computer-use stop shortcut (TD-5224).
//!
//! A global shortcut that works while TST Desk is in the background and
//! creates the machine-wide stop-file (TD-5223), the same file the title-bar
//! Stop writes. Every computer-use client refuses input while it exists, and
//! the daemon shows it as stopped within a second.
//!
//! Engage only. The shortcut never resumes: an agent can press key combos,
//! so a toggle would let it switch itself back on. Resume stays a click in
//! TST Desk.
//!
//! The combo is `computer_use.stop_shortcut` in `config.yaml` (default
//! `Ctrl+Alt+Super+Period`, ⌃⌥⌘. on macOS). Empty turns it off. It is
//! read at launch.

use std::path::{Path, PathBuf};

use crate::cu_stop::{self, Sources};

/// ⌃⌥⌘. on macOS; Ctrl+Alt+Super+. elsewhere. Unlike ⌘. (Cancel in many
/// apps) nothing else claims it.
pub const DEFAULT_SHORTCUT: &str = "Ctrl+Alt+Super+Period";

/// What `config.yaml` asks for.
#[derive(Debug, PartialEq, Eq)]
pub enum Setting {
    /// Register this combo.
    Combo(String),
    /// `stop_shortcut: ""`: no system-wide shortcut.
    Off,
}

/// `computer_use.stop_shortcut` from the config text; the default when unset.
pub fn setting_from_config(config_text: Option<&str>) -> Setting {
    let value = config_text
        .and_then(|text| serde_yml::from_str::<serde_yml::Value>(text).ok())
        .and_then(|doc| {
            doc.get("computer_use")?
                .get("stop_shortcut")
                .map(|v| v.as_str().map(str::to_owned))
        });
    match value {
        // Present but not a string (null, a number): fall back to the default.
        None | Some(None) => Setting::Combo(DEFAULT_SHORTCUT.into()),
        Some(Some(text)) if text.trim().is_empty() => Setting::Off,
        Some(Some(text)) => Setting::Combo(text.trim().to_string()),
    }
}

pub fn setting(data_dir: &Path) -> Setting {
    let text = std::fs::read_to_string(data_dir.join("config.yaml")).ok();
    setting_from_config(text.as_deref())
}

/// Create the stop-file for these sources. Never removes it.
pub fn engage(sources: &Sources) -> std::io::Result<PathBuf> {
    let path = cu_stop::resolve(sources).ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::NotFound, "no home directory")
    })?;
    cu_stop::engage_at(&path)?;
    Ok(path)
}

/// Register the shortcut. The global-shortcut plugin must already be built.
pub fn setup(app: &tauri::AppHandle, data_dir: &Path) {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

    let combo = match setting(data_dir) {
        Setting::Off => {
            log::info!("computer-use stop shortcut is off (computer_use.stop_shortcut)");
            return;
        }
        Setting::Combo(combo) => combo,
    };
    let shortcut: Shortcut = match combo.parse() {
        Ok(shortcut) => shortcut,
        Err(err) => {
            log::warn!("computer-use stop shortcut {combo:?} is not a valid combo: {err}");
            return;
        }
    };
    let registered = app
        .global_shortcut()
        .on_shortcut(shortcut, |_app, _shortcut, event| {
            if event.state() != ShortcutState::Pressed {
                return;
            }
            match engage(&Sources::live()) {
                Ok(path) => log::info!("computer use stopped by shortcut: {}", path.display()),
                Err(err) => log::error!("computer-use stop shortcut could not stop: {err}"),
            }
        });
    match registered {
        Ok(()) => log::info!("computer-use stop shortcut registered: {combo}"),
        Err(err) => log::warn!("computer-use stop shortcut {combo:?} not registered: {err}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};
    use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut};

    static N: AtomicU32 = AtomicU32::new(0);

    fn temp_home() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "tst-stop-key-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn sources(home: &Path) -> Sources {
        Sources {
            home: Some(home.to_path_buf()),
            ..Sources::default()
        }
    }

    #[test]
    fn default_when_unset() {
        assert_eq!(
            setting_from_config(None),
            Setting::Combo(DEFAULT_SHORTCUT.into())
        );
        assert_eq!(
            setting_from_config(Some("computer_use:\n  command: ''\n")),
            Setting::Combo(DEFAULT_SHORTCUT.into())
        );
        assert_eq!(
            setting_from_config(Some("computer_use:\n  stop_shortcut:\n")),
            Setting::Combo(DEFAULT_SHORTCUT.into())
        );
    }

    #[test]
    fn configured_combo_and_off() {
        assert_eq!(
            setting_from_config(Some("computer_use:\n  stop_shortcut: Ctrl+Shift+F12\n")),
            Setting::Combo("Ctrl+Shift+F12".into())
        );
        assert_eq!(
            setting_from_config(Some("computer_use:\n  stop_shortcut: ''\n")),
            Setting::Off
        );
    }

    #[test]
    fn default_is_ctrl_alt_super_period_not_bare_cmd_period() {
        let parsed: Shortcut = DEFAULT_SHORTCUT.parse().unwrap();
        assert_eq!(
            parsed,
            Shortcut::new(
                Some(Modifiers::CONTROL | Modifiers::ALT | Modifiers::SUPER),
                Code::Period
            )
        );
        let cmd_period = Shortcut::new(Some(Modifiers::SUPER), Code::Period);
        assert_ne!(parsed, cmd_period);
    }

    #[test]
    fn engage_creates_the_stop_file() {
        let home = temp_home();
        let s = sources(&home);
        assert!(!cu_stop::engaged_for(&s));
        let path = engage(&s).unwrap();
        assert_eq!(path, home.join(".tst-cu-mcp").join("STOP"));
        assert!(cu_stop::engaged_for(&s));
    }

    #[test]
    fn engage_never_resumes() {
        // Pressing it again while stopped (an agent mashing the combo) keeps
        // it stopped: there is no toggle.
        let home = temp_home();
        let s = sources(&home);
        engage(&s).unwrap();
        engage(&s).unwrap();
        assert!(cu_stop::engaged_for(&s));
    }

    #[test]
    fn engage_follows_the_configured_stop_file() {
        let home = temp_home();
        let config = home.join("cu.yaml");
        let target = home.join("elsewhere").join("STOP");
        std::fs::write(
            &config,
            format!("killswitch:\n  stop_file: {}\n", target.display()),
        )
        .unwrap();
        let s = Sources {
            config_env: Some(config.display().to_string()),
            ..sources(&home)
        };
        assert_eq!(engage(&s).unwrap(), target);
        assert!(target.exists());
    }
}
